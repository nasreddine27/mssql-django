# Copyright (c) Microsoft Corporation.
# Licensed under the BSD license.

import types
from itertools import chain

import django
from django.db.models.aggregates import Avg, Count, StdDev, Variance
from django.db.models.expressions import Ref, Subquery, Value, Window
from django.db.models.functions import (
    Chr, ConcatPair, Greatest, Least, Length, LPad, Random, Repeat, RPad, StrIndex, Substr, Trim
)
from django.db.models.sql import compiler
from django.db.transaction import TransactionManagementError
from django.db.utils import NotSupportedError
if django.VERSION >= (3, 1):
    from django.db.models.fields.json import compile_json_path, KeyTransform as json_KeyTransform
if django.VERSION >= (4, 2):
    from django.core.exceptions import EmptyResultSet, FullResultSet

from .introspection import get_table_name, get_schema_name
from django.apps import apps

def _as_sql_agv(self, compiler, connection):
    return self.as_sql(compiler, connection, template='%(function)s(CONVERT(float, %(field)s))')


def _as_sql_chr(self, compiler, connection):
    return self.as_sql(compiler, connection, function='NCHAR')


def _as_sql_concatpair(self, compiler, connection):
    if connection.sql_server_version < 2012:
        node = self.coalesce()
        return node.as_sql(compiler, connection, arg_joiner=' + ', template='%(expressions)s')
    else:
        return self.as_sql(compiler, connection)


def _as_sql_count(self, compiler, connection):
    return self.as_sql(compiler, connection, function='COUNT_BIG')


def _as_sql_greatest(self, compiler, connection):
    # SQL Server does not provide GREATEST function,
    # so we emulate it with a table value constructor
    # https://msdn.microsoft.com/en-us/library/dd776382.aspx
    template = '(SELECT MAX(value) FROM (VALUES (%(expressions)s)) AS _%(function)s(value))'
    return self.as_sql(compiler, connection, arg_joiner='), (', template=template)

def _as_sql_json_keytransform(self, compiler, connection):
    lhs, params, key_transforms = self.preprocess_lhs(compiler, connection)
    json_path = compile_json_path(key_transforms)
    return (
        "COALESCE(JSON_QUERY(%s, '%s'), JSON_VALUE(%s, '%s'))" %
        ((lhs, json_path) * 2)
    ), tuple(params) * 2

def _as_sql_least(self, compiler, connection):
    # SQL Server does not provide LEAST function,
    # so we emulate it with a table value constructor
    # https://msdn.microsoft.com/en-us/library/dd776382.aspx
    template = '(SELECT MIN(value) FROM (VALUES (%(expressions)s)) AS _%(function)s(value))'
    return self.as_sql(compiler, connection, arg_joiner='), (', template=template)


def _as_sql_length(self, compiler, connection):
    return self.as_sql(compiler, connection, function='LEN')


def _as_sql_lpad(self, compiler, connection):
    i = iter(self.get_source_expressions())
    expression, expression_arg = compiler.compile(next(i))
    length, length_arg = compiler.compile(next(i))
    fill_text, fill_text_arg = compiler.compile(next(i))
    params = []
    params.extend(fill_text_arg)
    params.extend(length_arg)
    params.extend(length_arg)
    params.extend(expression_arg)
    params.extend(length_arg)
    params.extend(expression_arg)
    params.extend(expression_arg)
    params.extend(length_arg)
    template = ('LEFT(LEFT(REPLICATE(%(fill_text)s, %(length)s), CASE WHEN %(length)s > LEN(%(expression)s) '
                'THEN %(length)s - LEN(%(expression)s) ELSE 0 END) + %(expression)s, %(length)s)')
    return template % {'expression': expression, 'length': length, 'fill_text': fill_text}, params


def _as_sql_repeat(self, compiler, connection):
    return self.as_sql(compiler, connection, function='REPLICATE')


def _as_sql_rpad(self, compiler, connection):
    i = iter(self.get_source_expressions())
    expression, expression_arg = compiler.compile(next(i))
    length, length_arg = compiler.compile(next(i))
    fill_text, fill_text_arg = compiler.compile(next(i))
    params = []
    params.extend(expression_arg)
    params.extend(fill_text_arg)
    params.extend(length_arg)
    params.extend(length_arg)
    template = 'LEFT(%(expression)s + REPLICATE(%(fill_text)s, %(length)s), %(length)s)'
    return template % {'expression': expression, 'length': length, 'fill_text': fill_text}, params


def _as_sql_stddev(self, compiler, connection):
    function = 'STDEV'
    if self.function == 'STDDEV_POP':
        function = '%sP' % function
    return self.as_sql(compiler, connection, function=function)


def _as_sql_strindex(self, compiler, connection):
    self.source_expressions.reverse()
    sql = self.as_sql(compiler, connection, function='CHARINDEX')
    self.source_expressions.reverse()
    return sql


def _as_sql_substr(self, compiler, connection):
    if len(self.get_source_expressions()) < 3:
        self.get_source_expressions().append(Value(2**31 - 1))
    return self.as_sql(compiler, connection)


def _as_sql_trim(self, compiler, connection):
    return self.as_sql(compiler, connection, template='LTRIM(RTRIM(%(expressions)s))')


def _as_sql_variance(self, compiler, connection):
    function = 'VAR'
    if self.function == 'VAR_POP':
        function = '%sP' % function
    return self.as_sql(compiler, connection, function=function)

def _as_sql_window(self, compiler, connection, template=None):
    connection.ops.check_expression_support(self)
    if not connection.features.supports_over_clause:
        raise NotSupportedError("This backend does not support window expressions.")
    expr_sql, params = compiler.compile(self.source_expression)
    window_sql, window_params = [], ()

    if self.partition_by is not None:
        sql_expr, sql_params = self.partition_by.as_sql(
            compiler=compiler,
            connection=connection,
            template="PARTITION BY %(expressions)s",
        )
        window_sql.append(sql_expr)
        window_params += tuple(sql_params)

    if self.order_by is not None:
        order_sql, order_params = compiler.compile(self.order_by)
        window_sql.append(order_sql)
        window_params += tuple(order_params)
    else:
        # MSSQL window functions require an OVER clause with ORDER BY
        window_sql.append('ORDER BY (SELECT NULL)')

    if self.frame:
        frame_sql, frame_params = compiler.compile(self.frame)
        window_sql.append(frame_sql)
        window_params += tuple(frame_params)

    template = template or self.template

    return (
        template % {"expression": expr_sql, "window": " ".join(window_sql).strip()},
        (*params, *window_params),
    )

def _cursor_iter(cursor, sentinel, col_count, itersize):
    """
    Yields blocks of rows from a cursor and ensures the cursor is closed when
    done.
    """
    if not hasattr(cursor.db, 'supports_mars') or cursor.db.supports_mars:
        # same as the original Django implementation
        try:
            for rows in iter((lambda: cursor.fetchmany(itersize)), sentinel):
                yield rows if col_count is None else [r[:col_count] for r in rows]
        finally:
            cursor.close()
    else:
        # retrieve all chunks from the cursor and close it before yielding
        # so that we can open an another cursor over an iteration
        # (for drivers such as FreeTDS)
        chunks = []
        try:
            for rows in iter((lambda: cursor.fetchmany(itersize)), sentinel):
                chunks.append(rows if col_count is None else [r[:col_count] for r in rows])
        finally:
            cursor.close()
        for rows in chunks:
            yield rows


compiler.cursor_iter = _cursor_iter

class SQLCompiler(compiler.SQLCompiler):

    def as_sql(self, with_limits=True, with_col_aliases=False):
        """
        Create the SQL for this query. Return the SQL string and list of
        parameters.

        If 'with_limits' is False, any limit/offset information is not included
        in the query.
        """
        refcounts_before = self.query.alias_refcount.copy()
        try:
            extra_select, order_by, group_by = self.pre_sql_setup()
            for_update_part = None
            # Is a LIMIT/OFFSET clause needed?
            with_limit_offset = with_limits and (self.query.high_mark is not None or self.query.low_mark)
            combinator = self.query.combinator
            features = self.connection.features

            # The do_offset flag indicates whether we need to construct
            # the SQL needed to use limit/offset w/SQL Server.
            high_mark = self.query.high_mark
            low_mark = self.query.low_mark
            do_limit = with_limits and high_mark is not None
            do_offset = with_limits and low_mark != 0
            # SQL Server 2012 or newer supports OFFSET/FETCH clause
            supports_offset_clause = self.connection.sql_server_version >= 2012
            do_offset_emulation = do_offset and not supports_offset_clause

            if combinator:

                if not getattr(features, 'supports_select_{}'.format(combinator)):
                    raise NotSupportedError('{} is not supported on this database backend.'.format(combinator))
                result, params = self.get_combinator_sql(combinator, self.query.combinator_all)
            elif django.VERSION >= (4, 2) and self.qualify:
                result, params = self.get_qualify_sql()
                order_by = None
            else:
                distinct_fields, distinct_params = self.get_distinct()
                # This must come after 'select', 'ordering', and 'distinct' -- see
                # docstring of get_from_clause() for details.
                from_, f_params = self.get_from_clause()
                if django.VERSION >= (4, 2):
                    try:
                        where, w_params = self.compile(self.where) if self.where is not None else ("", [])
                    except EmptyResultSet:
                        if self.elide_empty:
                            raise
                        # Use a predicate that's always False.
                        where, w_params = "0 = 1", []
                    except FullResultSet:
                        where, w_params = "", []
                    try:
                        having, h_params = self.compile(self.having) if self.having is not None else ("", [])
                    except FullResultSet:
                        having, h_params = "", []
                else:
                    where, w_params = self.compile(self.where) if self.where is not None else ("", [])
                    having, h_params = self.compile(self.having) if self.having is not None else ("", [])
                params = []
                result = ['SELECT']

                if self.query.distinct:
                    distinct_result, distinct_params = self.connection.ops.distinct_sql(
                        distinct_fields,
                        distinct_params,
                    )
                    result += distinct_result
                    params += distinct_params

                # SQL Server requires the keword for limitting at the begenning
                if do_limit and not do_offset:
                    result.append('TOP %d' % high_mark)

                out_cols = []
                col_idx = 1
                for _, (s_sql, s_params), alias in self.select + extra_select:
                    if alias:
                        s_sql = '%s AS %s' % (s_sql, self.connection.ops.quote_name(alias))
                    elif with_col_aliases or do_offset_emulation:
                        s_sql = '%s AS %s' % (s_sql, 'Col%d' % col_idx)
                        col_idx += 1
                    params.extend(s_params)
                    out_cols.append(s_sql)

                # SQL Server requires an order-by clause for offsetting
                if do_offset:
                    meta = self.query.get_meta()
                    qn = self.quote_name_unless_alias
                    table = qn(get_table_name(self, meta.db_table, getattr(meta, "db_table_schema", False)))
                    offsetting_order_by = '%s.%s' % (table, qn(meta.pk.db_column or meta.pk.column))
                    if do_offset_emulation:
                        if order_by:
                            ordering = []
                            for expr, (o_sql, o_params, _) in order_by:
                                # value_expression in OVER clause cannot refer to
                                # expressions or aliases in the select list. See:
                                # http://msdn.microsoft.com/en-us/library/ms189461.aspx
                                src = next(iter(expr.get_source_expressions()))
                                if isinstance(src, Ref):
                                    src = next(iter(src.get_source_expressions()))
                                    o_sql, _ = src.as_sql(self, self.connection)
                                    odir = 'DESC' if expr.descending else 'ASC'
                                    o_sql = '%s %s' % (o_sql, odir)
                                ordering.append(o_sql)
                                params.extend(o_params)
                            offsetting_order_by = ', '.join(ordering)
                            order_by = []
                        out_cols.append('ROW_NUMBER() OVER (ORDER BY %s) AS [rn]' % offsetting_order_by)
                    elif not order_by:
                        order_by.append(((None, ('%s ASC' % offsetting_order_by, [], None))))

                if self.query.select_for_update and self.connection.features.has_select_for_update:
                    if self.connection.get_autocommit():
                        raise TransactionManagementError('select_for_update cannot be used outside of a transaction.')

                    if with_limit_offset and not self.connection.features.supports_select_for_update_with_limit:
                        raise NotSupportedError(
                            'LIMIT/OFFSET is not supported with '
                            'select_for_update on this database backend.'
                        )
                    nowait = self.query.select_for_update_nowait
                    skip_locked = self.query.select_for_update_skip_locked
                    of = self.query.select_for_update_of
                    # If it's a NOWAIT/SKIP LOCKED/OF query but the backend
                    # doesn't support it, raise NotSupportedError to prevent a
                    # possible deadlock.
                    if nowait and not self.connection.features.has_select_for_update_nowait:
                        raise NotSupportedError('NOWAIT is not supported on this database backend.')
                    elif skip_locked and not self.connection.features.has_select_for_update_skip_locked:
                        raise NotSupportedError('SKIP LOCKED is not supported on this database backend.')
                    elif of and not self.connection.features.has_select_for_update_of:
                        raise NotSupportedError('FOR UPDATE OF is not supported on this database backend.')
                    for_update_part = self.connection.ops.for_update_sql(
                        nowait=nowait,
                        skip_locked=skip_locked,
                        of=self.get_select_for_update_of_arguments(),
                    )

                if for_update_part and self.connection.features.for_update_after_from:
                    from_.insert(1, for_update_part)

                result += [', '.join(out_cols)]
                if from_:
                    result += ['FROM', *from_]
                params.extend(f_params)

                if where:
                    result.append('WHERE %s' % where)
                    params.extend(w_params)

                grouping = []
                for g_sql, g_params in group_by:
                    grouping.append(g_sql)
                    params.extend(g_params)
                if grouping:
                    if distinct_fields:
                        raise NotImplementedError('annotate() + distinct(fields) is not implemented.')
                    order_by = order_by or self.connection.ops.force_no_ordering()
                    result.append('GROUP BY %s' % ', '.join(grouping))

                if having:
                    result.append('HAVING %s' % having)
                    params.extend(h_params)

            explain = self.query.explain_info if django.VERSION >= (4, 0) else self.query.explain_query
            if explain:
                result.insert(0, self.connection.ops.explain_query_prefix(
                    self.query.explain_format,
                    **self.query.explain_options
                ))

            if order_by:
                ordering = []
                for expr, (o_sql, o_params, _) in order_by:
                    if expr:
                        src = next(iter(expr.get_source_expressions()))
                        if isinstance(src, Random):
                            # ORDER BY RAND() doesn't return rows in random order
                            # replace it with NEWID()
                            o_sql = o_sql.replace('RAND()', 'NEWID()')
                    ordering.append(o_sql)
                    params.extend(o_params)
                result.append('ORDER BY %s' % ', '.join(ordering))

                # For subqueres with an ORDER BY clause, SQL Server also
                # requires a TOP or OFFSET clause which is not generated for
                # Django 2.x.  See https://github.com/microsoft/mssql-django/issues/12
                # Add OFFSET for all Django versions.
                # https://github.com/microsoft/mssql-django/issues/109
                if not (do_offset or do_limit) and supports_offset_clause:
                    result.append("OFFSET 0 ROWS")

            # SQL Server requires the backend-specific emulation (2008 or earlier)
            # or an offset clause (2012 or newer) for offsetting
            if do_offset:
                if do_offset_emulation:
                    # Construct the final SQL clause, using the initial select SQL
                    # obtained above.
                    result = ['SELECT * FROM (%s) AS X WHERE X.rn' % ' '.join(result)]
                    # Place WHERE condition on `rn` for the desired range.
                    if do_limit:
                        result.append('BETWEEN %d AND %d' % (low_mark + 1, high_mark))
                    else:
                        result.append('>= %d' % (low_mark + 1))
                    if not self.query.subquery:
                        result.append('ORDER BY X.rn')
                else:
                    result.append(self.connection.ops.limit_offset_sql(self.query.low_mark, self.query.high_mark))

            if self.query.subquery and extra_select:
                # If the query is used as a subquery, the extra selects would
                # result in more columns than the left-hand side expression is
                # expecting. This can happen when a subquery uses a combination
                # of order_by() and distinct(), forcing the ordering expressions
                # to be selected as well. Wrap the query in another subquery
                # to exclude extraneous selects.
                sub_selects = []
                sub_params = []
                for index, (select, _, alias) in enumerate(self.select, start=1):
                    if not alias and with_col_aliases:
                        alias = 'col%d' % index
                    if alias:
                        sub_selects.append("%s.%s" % (
                            self.connection.ops.quote_name('subquery'),
                            self.connection.ops.quote_name(alias),
                        ))
                    else:
                        select_clone = select.relabeled_clone({select.alias: 'subquery'})
                        subselect, subparams = select_clone.as_sql(self, self.connection)
                        sub_selects.append(subselect)
                        sub_params.extend(subparams)
                return 'SELECT %s FROM (%s) subquery' % (
                    ', '.join(sub_selects),
                    ' '.join(result),
                ), tuple(sub_params + params)
            return ' '.join(result), tuple(params)
        finally:
            # Finally do cleanup - get rid of the joins we created above.
            self.query.reset_refcounts(refcounts_before)
    def get_from_clause(self):
        """
        Return a list of strings that are joined together to go after the
        "FROM" part of the query, as well as a list any extra parameters that
        need to be included. Subclasses, can override this to create a
        from-clause via a "select".

        This should only be called after any SQL construction methods that
        might change the tables that are needed. This means the select columns,
        ordering, and distinct must be done first.
        """
        result = []
        params = []
        for alias in tuple(self.query.alias_map):
            if not self.query.alias_refcount[alias]:
                continue
            try:
                from_clause = self.query.alias_map[alias]
            except KeyError:
                # Extra tables can end up in self.tables, but not in the
                # alias_map if they aren't in a join. That's OK. We skip them.
                continue
            settings_dict = self.connection.settings_dict
            clause_sql, clause_params = self.compile(from_clause)
            model = next((m for m in apps.get_models() if m._meta.db_table == from_clause.table_name), None)
            schema = getattr(getattr(model,"_meta", None), "db_table_schema", settings_dict.get('SCHEMA', False))
            if schema:
                if 'JOIN' in clause_sql:
                    table_clause_sql = clause_sql.split('JOIN ')[1].split(' ON')[0]
                    table_clause_sql = f'[{schema}].{table_clause_sql}'
                    clause_sql = clause_sql.split('JOIN')[0] + 'JOIN ' + table_clause_sql + ' ON' + clause_sql.split('JOIN')[1].split('ON')[1]
                else:
                    clause_sql = f'[{schema}].{clause_sql}'
            result.append(clause_sql)
            params.extend(clause_params)
        for t in self.query.extra_tables:
            alias, _ = self.query.table_alias(t)
            # Only add the alias if it's not already present (the table_alias()
            # call increments the refcount, so an alias refcount of one means
            # this is the only reference).
            if alias not in self.query.alias_map or self.query.alias_refcount[alias] == 1:
                result.append(', %s' % self.quote_name_unless_alias(alias))
        return result, params
    def compile(self, node, *args, **kwargs):
        node = self._as_microsoft(node)
        return super().compile(node, *args, **kwargs)

    def collapse_group_by(self, expressions, having):
        expressions = super().collapse_group_by(expressions, having)
        # SQL server does not allow subqueries or constant expressions in the group by
        # For constants: Each GROUP BY expression must contain at least one column that is not an outer reference.
        # For subqueries: Cannot use an aggregate or a subquery in an expression used for the group by list of a GROUP BY clause.
        return self._filter_subquery_and_constant_expressions(expressions)

    def _is_constant_expression(self, expression):
        if isinstance(expression, Value):
            return True
        sub_exprs = expression.get_source_expressions()
        if not sub_exprs:
            return False
        for each in sub_exprs:
            if not self._is_constant_expression(each):
                return False
        return True



    def _filter_subquery_and_constant_expressions(self, expressions):
        ret = []
        for expression in expressions:
            if self._is_subquery(expression):
                continue
            if self._is_constant_expression(expression):
                continue
            if not self._has_nested_subquery(expression):
                ret.append(expression)
        return ret

    def _has_nested_subquery(self, expression):
        if self._is_subquery(expression):
            return True
        for sub_expr in expression.get_source_expressions():
            if self._has_nested_subquery(sub_expr):
                return True
        return False

    def _is_subquery(self, expression):
        return isinstance(expression, Subquery)

    def _as_microsoft(self, node):
        as_microsoft = None
        if isinstance(node, Avg):
            as_microsoft = _as_sql_agv
        elif isinstance(node, Chr):
            as_microsoft = _as_sql_chr
        elif isinstance(node, ConcatPair):
            as_microsoft = _as_sql_concatpair
        elif isinstance(node, Count):
            as_microsoft = _as_sql_count
        elif isinstance(node, Greatest):
            as_microsoft = _as_sql_greatest
        elif isinstance(node, Least):
            as_microsoft = _as_sql_least
        elif isinstance(node, Length):
            as_microsoft = _as_sql_length
        elif isinstance(node, RPad):
            as_microsoft = _as_sql_rpad
        elif isinstance(node, LPad):
            as_microsoft = _as_sql_lpad
        elif isinstance(node, Repeat):
            as_microsoft = _as_sql_repeat
        elif isinstance(node, StdDev):
            as_microsoft = _as_sql_stddev
        elif isinstance(node, StrIndex):
            as_microsoft = _as_sql_strindex
        elif isinstance(node, Substr):
            as_microsoft = _as_sql_substr
        elif isinstance(node, Trim):
            as_microsoft = _as_sql_trim
        elif isinstance(node, Variance):
            as_microsoft = _as_sql_variance
        if django.VERSION >= (3, 1):
            if isinstance(node, json_KeyTransform):
                as_microsoft = _as_sql_json_keytransform
        if django.VERSION >= (4, 1):
            if isinstance(node, Window):
                as_microsoft = _as_sql_window
        if as_microsoft:
            node = node.copy()
            node.as_microsoft = types.MethodType(as_microsoft, node)
        return node


class SQLInsertCompiler(compiler.SQLInsertCompiler, SQLCompiler):
    def get_returned_fields(self):
        if django.VERSION >= (3, 0, 0):
            return self.returning_fields
        return self.return_id

    def can_return_columns_from_insert(self):
        if django.VERSION >= (3, 0, 0):
            return self.connection.features.can_return_columns_from_insert
        return self.connection.features.can_return_id_from_insert

    def can_return_rows_from_bulk_insert(self):
        if django.VERSION >= (3, 0, 0):
            return self.connection.features.can_return_rows_from_bulk_insert
        return self.connection.features.can_return_ids_from_bulk_insert

    def fix_auto(self, sql, opts, fields, qn):
        if opts.auto_field is not None:
            # db_column is None if not explicitly specified by model field
            auto_field_column = opts.auto_field.db_column or opts.auto_field.column
            columns = [f.column for f in fields]
            if auto_field_column in columns:
                id_insert_sql = []
                table = qn(get_table_name(self, opts.db_table, getattr(opts, "db_table_schema", False)))
                sql_format = 'SET IDENTITY_INSERT %s ON; %s; SET IDENTITY_INSERT %s OFF'
                for q, p in sql:
                    id_insert_sql.append((sql_format % (table, q, table), p))
                sql = id_insert_sql

        return sql

    def bulk_insert_default_values_sql(self, table):
        seed_rows_number = 8
        cross_join_power = 4  # 8^4 = 4096 > maximum allowed batch size for the backend = 1000

        def generate_seed_rows(n):
            return " UNION ALL ".join("SELECT 1 AS x" for _ in range(n))

        def cross_join(p):
            return ", ".join("SEED_ROWS AS _%s" % i for i in range(p))

        return """
        WITH SEED_ROWS AS (%s)
            MERGE INTO %s
            USING (
                SELECT TOP %s * FROM (SELECT 1 as x FROM %s) FAKE_ROWS
            ) FAKE_DATA
            ON 1 = 0
            WHEN NOT MATCHED THEN
            INSERT DEFAULT VALUES
        """ % (generate_seed_rows(seed_rows_number),
               table,
               len(self.query.objs),
               cross_join(cross_join_power))

    def as_sql(self):
        # We don't need quote_name_unless_alias() here, since these are all
        # going to be column names (so we can avoid the extra overhead).
        qn = self.connection.ops.quote_name
        opts = self.query.get_meta()
        table = qn(get_table_name(self, opts.db_table, getattr(opts, "db_table_schema", False)))
        result = ['INSERT INTO %s' % table]

        if self.query.fields:
            fields = self.query.fields
            result.append('(%s)' % ', '.join(qn(f.column) for f in fields))
            values_format = 'VALUES (%s)'
            value_rows = [
                [self.prepare_value(field, self.pre_save_val(field, obj)) for field in fields]
                for obj in self.query.objs
            ]
        else:
            values_format = '%s VALUES'
            # An empty object.
            value_rows = [[self.connection.ops.pk_default_value()] for _ in self.query.objs]
            fields = [None]

        # Currently the backends just accept values when generating bulk
        # queries and generate their own placeholders. Doing that isn't
        # necessary and it should be possible to use placeholders and
        # expressions in bulk inserts too.
        can_bulk = (not self.get_returned_fields() and self.connection.features.has_bulk_insert) and self.query.fields

        placeholder_rows, param_rows = self.assemble_as_sql(fields, value_rows)

        if self.get_returned_fields() and self.can_return_columns_from_insert():
            if self.can_return_rows_from_bulk_insert():
                if not(self.query.fields):
                    # There isn't really a single statement to bulk multiple DEFAULT VALUES insertions,
                    # so we have to use a workaround:
                    # https://dba.stackexchange.com/questions/254771/insert-multiple-rows-into-a-table-with-only-an-identity-column
                    result = [self.bulk_insert_default_values_sql(qn(table))]
                    r_sql, self.returning_params = self.connection.ops.return_insert_columns(self.get_returned_fields())
                    if r_sql:
                        result.append(r_sql)
                    sql = " ".join(result) + ";"
                    return [(sql, None)]
                # Regular bulk insert
                params = []
                r_sql, self.returning_params = self.connection.ops.return_insert_columns(self.get_returned_fields())
                if r_sql:
                    result.append(r_sql)
                    params += [self.returning_params]
                params += param_rows
                result.append(self.connection.ops.bulk_insert_sql(fields, placeholder_rows))
            else:
                result.insert(0, 'SET NOCOUNT ON')
                result.append((values_format + ';') % ', '.join(placeholder_rows[0]))
                params = [param_rows[0]]
                result.append('SELECT CAST(SCOPE_IDENTITY() AS bigint)')
            sql = [(" ".join(result), tuple(chain.from_iterable(params)))]
        else:
            if can_bulk:
                result.append(self.connection.ops.bulk_insert_sql(fields, placeholder_rows))
                sql = [(" ".join(result), tuple(p for ps in param_rows for p in ps))]
            else:
                sql = [
                    (" ".join(result + [values_format % ", ".join(p)]), vals)
                    for p, vals in zip(placeholder_rows, param_rows)
                ]

        if self.query.fields:
            sql = self.fix_auto(sql, opts, fields, qn)

        return sql


class SQLDeleteCompiler(compiler.SQLDeleteCompiler, SQLCompiler):
    def as_sql(self):
        sql, params = super().as_sql()
        if sql:
            sql = '; '.join(['SET NOCOUNT OFF', sql])
        return sql, params

    def _as_sql(self, query):
        opts = self.query.get_meta()
        table = get_table_name(self, query.base_table, getattr(opts, "db_table_schema", False))
        delete = "DELETE FROM %s" % self.quote_name_unless_alias(table)
        try:
            where, params = self.compile(query.where)
        except FullResultSet:
            return delete, ()
        return f"{delete} WHERE {where}", tuple(params)


class SQLUpdateCompiler(compiler.SQLUpdateCompiler, SQLCompiler):
    def as_sql(self):
        """
        Create the SQL for this query. Return the SQL string and list of
        parameters.
        """
        self.pre_sql_setup()
        if not self.query.values:
            return "", ()
        qn = self.quote_name_unless_alias
        values, update_params = [], []
        for field, model, val in self.query.values:
            if hasattr(val, "resolve_expression"):
                val = val.resolve_expression(
                    self.query, allow_joins=False, for_save=True
                )
                if val.contains_aggregate:
                    raise FieldError(
                        "Aggregate functions are not allowed in this query "
                        "(%s=%r)." % (field.name, val)
                    )
                if val.contains_over_clause:
                    raise FieldError(
                        "Window expressions are not allowed in this query "
                        "(%s=%r)." % (field.name, val)
                    )
            elif hasattr(val, "prepare_database_save"):
                if field.remote_field:
                    val = val.prepare_database_save(field)
                else:
                    raise TypeError(
                        "Tried to update field %s with a model instance, %r. "
                        "Use a value compatible with %s."
                        % (field, val, field.__class__.__name__)
                    )
            val = field.get_db_prep_save(val, connection=self.connection)

            # Getting the placeholder for the field.
            if hasattr(field, "get_placeholder"):
                placeholder = field.get_placeholder(val, self, self.connection)
            else:
                placeholder = "%s"
            name = field.column
            if hasattr(val, "as_sql"):
                sql, params = self.compile(val)
                values.append("%s = %s" % (qn(name), placeholder % sql))
                update_params.extend(params)
            elif val is not None:
                values.append("%s = %s" % (qn(name), placeholder))
                update_params.append(val)
            else:
                values.append("%s = NULL" % qn(name))
        opts = self.query.get_meta()
        table = get_table_name(self, self.query.base_table, getattr(opts, "db_table_schema", False))
        result = [
            "UPDATE %s SET" % qn(table),
            ", ".join(values),
        ]
        try:
            where, params = self.compile(self.query.where)
        except FullResultSet:
            params = []
        else:
            result.append("WHERE %s" % where)
        return " ".join(result), tuple(update_params + params)


class SQLAggregateCompiler(compiler.SQLAggregateCompiler, SQLCompiler):
    pass
