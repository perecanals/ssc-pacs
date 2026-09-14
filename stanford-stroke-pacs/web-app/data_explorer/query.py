"""Strict PostgreSQL SELECT validation and parameterized visual query builder."""
from pglast import ast, parse_sql
from pglast.stream import RawStream
from pglast.visitors import Visitor

from data_explorer.policy import RELATIONSHIPS

FUNCTIONS = set("count sum avg min max lower upper length char_length trim btrim ltrim rtrim "
                "abs round ceil ceiling floor date_part date_trunc to_char concat concat_ws "
                "replace substring array_length cardinality array_to_string jsonb_extract_path_text "
                "jsonb_typeof string_agg array_agg bool_and bool_or row_number rank dense_rank".split())
TYPES = set("text varchar bpchar bool int2 int4 int8 numeric float4 float8 date timestamp timestamptz "
            "interval time timetz uuid json jsonb".split())
NODES = set("SelectStmt ResTarget ColumnRef A_Star RangeVar Alias JoinExpr RangeSubselect "
            "A_Expr A_Const String Integer Float Boolean BitString BoolExpr NullTest BooleanTest "
            "FuncCall CoalesceExpr MinMaxExpr CaseExpr CaseWhen TypeCast TypeName SortBy "
            "WithClause CommonTableExpr SubLink A_ArrayExpr RowExpr WindowDef "
            "GroupingSet A_Indirection A_Indices List".split())
OPERATORS = set("= <> != < > <= >= + - * / % ~~ ~~* !~~ !~~* @> <@ && -> ->> #> #>> ? ?| ?& ||".split())


def validate_sql(source: str, tables: set[str]) -> str:
    if not source.strip() or len(source) > 100_000:
        raise ValueError("SQL must contain 1–100,000 characters")
    try:
        parsed = parse_sql(source)
    except Exception:
        raise ValueError("Invalid PostgreSQL SQL syntax") from None
    if len(parsed) != 1 or not isinstance(parsed[0].stmt, ast.SelectStmt):
        raise ValueError("Only a single SELECT query is supported")

    class Qualify(Visitor):
        def visit(self, ancestors, node):
            if type(node).__name__ not in NODES | {"RawStmt"}:
                raise ValueError(f"SQL construct {type(node).__name__} is not supported")
            if isinstance(node, ast.SelectStmt) and (node.intoClause or node.lockingClause):
                raise ValueError("SELECT INTO and row locking are not allowed")
            if isinstance(node, ast.WithClause) and node.recursive:
                raise ValueError("Recursive queries are not supported")
            if isinstance(node, ast.RangeVar):
                if node.catalogname or node.schemaname not in (None, "public"):
                    raise ValueError("Only approved public research tables are accessible")
            if isinstance(node, (ast.FuncCall, ast.TypeName)):
                is_function = isinstance(node, ast.FuncCall)
                parts = node.funcname if is_function else node.names
                names = [part.sval for part in parts]
                allowed = FUNCTIONS if is_function else TYPES
                if not names or names[-1] not in allowed or (len(names) > 1 and names[:-1] != ["pg_catalog"]):
                    raise ValueError("Unsupported function or type")
                qualified = (ast.String(sval="pg_catalog"), parts[-1])
                if is_function:
                    node.funcname = qualified
                else:
                    node.names = qualified
            if isinstance(node, ast.A_Expr):
                names = [part.sval for part in node.name or ()]
                if names and (len(names) != 1 or names[0] not in OPERATORS):
                    raise ValueError("Unsupported SQL operator")

    def scope(node, visible=frozenset()):
        if isinstance(node, (tuple, list)):
            for child in node:
                scope(child, visible)
        elif isinstance(node, ast.Node):
            if isinstance(node, ast.SelectStmt):
                names = set(visible)
                if node.withClause:
                    for cte in node.withClause.ctes:
                        scope(cte.ctequery, names)
                        names.add(cte.ctename)
                for field in node:
                    if field != "withClause":
                        scope(getattr(node, field), names)
            elif isinstance(node, ast.RangeVar):
                if node.schemaname is None and node.relname in visible:
                    return
                if node.relname not in tables:
                    raise ValueError(f"Table {node.relname} is not in the research catalog")
                node.schemaname = "public"
                node.inh = False
            else:
                for field in node:
                    scope(getattr(node, field), visible)
    Qualify()(parsed)
    scope(parsed)
    return RawStream()(parsed)


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def build_query(config, catalog):
    """Return SQL + parameters; all identifiers come from the live allowed catalog."""
    tables = {t["name"]: {c["name"]: c for c in t["columns"]} for t in catalog}
    base = config.get("table")
    if base not in tables:
        raise ValueError("Choose an available research table")
    selected_tables = [base]
    joins = []
    for target in config.get("joins", []):
        if target in selected_tables or target not in tables:
            raise ValueError("Invalid related table")
        match = None
        for left, lc, right, rc in RELATIONSHIPS:
            if left in selected_tables and right == target:
                match = left, lc, right, rc
            elif right in selected_tables and left == target:
                match = right, rc, left, lc
            if match:
                break
        if not match:
            raise ValueError("Choose a connected predefined relationship")
        left, lc, right, rc = match
        if lc not in tables[left] or rc not in tables[right]:
            raise ValueError("Relationship columns are unavailable")
        joins.append(f"LEFT JOIN ONLY public.{quote(right)} ON {quote(left)}.{quote(lc)} = {quote(right)}.{quote(rc)}")
        selected_tables.append(target)

    def column(ref):
        if not isinstance(ref, str) or "." not in ref:
            raise ValueError("Select a catalog column")
        table, name = ref.split(".", 1)
        if table not in selected_tables or name not in tables[table]:
            raise ValueError("Column is not in the selected tables")
        return f"{quote(table)}.{quote(name)}", tables[table][name]["type"]

    columns = config.get("columns", [])
    if not columns or len(columns) > 500 or len(set(columns)) != len(columns):
        raise ValueError("Choose 1–500 distinct columns")
    projection = [f"{column(ref)[0]} AS {quote(ref)}" for ref in columns]
    params = []

    def filters(group, depth=0):
        if depth > 5 or not isinstance(group, dict):
            raise ValueError("Invalid filter group")
        if "rules" in group:
            if group.get("op", "and") not in ("and", "or") or len(group["rules"]) > 50:
                raise ValueError("Invalid filter group")
            terms = [filters(rule, depth + 1) for rule in group["rules"]]
            return "(" + f" {group.get('op', 'and').upper()} ".join(terms) + ")" if terms else "TRUE"
        expr, dtype = column(group.get("column"))
        op = group.get("op")
        if op in ("is_null", "not_null"):
            return f"{expr} IS {'NOT ' if op == 'not_null' else ''}NULL"
        value = group.get("value")
        if isinstance(value, (dict, list)) or value is None:
            raise ValueError("Filter value must be a scalar")
        if op == "contains" and dtype.endswith("[]"):
            params.append(str(value))
            return f"%s = ANY({expr})"
        if op == "contains":
            if dtype not in ("text", "character varying", "character"):
                raise ValueError("Contains requires text or an array")
            params.append("%" + str(value).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
            return f"{expr} ILIKE %s"
        operators = {"eq": "=", "ne": "<>", "lt": "<", "le": "<=", "gt": ">", "ge": ">="}
        if op not in operators:
            raise ValueError("Unsupported filter operator")
        params.append(value)
        return f"{expr} {operators[op]} %s"

    where = filters(config.get("filters", {"rules": []}))
    order = []
    for item in config.get("sort", []):
        direction = item.get("direction", "asc")
        if direction not in ("asc", "desc"):
            raise ValueError("Invalid sort direction")
        order.append(f"{column(item['column'])[0]} {direction.upper()} NULLS LAST")
    if len(order) > 20:
        raise ValueError("Too many sort columns")
    source = f"SELECT {', '.join(projection)} FROM ONLY public.{quote(base)} {' '.join(joins)} WHERE {where}"
    if order:
        source += " ORDER BY " + ", ".join(order)
    return source, params
