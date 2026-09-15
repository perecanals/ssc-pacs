"""Apply the selected cohort to every physical research table in a query."""

from pglast import ast, parse_sql
from pglast.stream import RawStream

from data_explorer.policy import TABLES


def scoped_table(table, dataset_literal=None, allowed_literal=None):
    """dataset_literal must be produced by the reader cursor's mogrify."""
    if table not in TABLES:
        raise ValueError("Choose an available research table")
    checks = []
    if dataset_literal is not None:
        checks.append(f"{dataset_literal} = ANY(explorer_patient.dataset)")
    if allowed_literal is not None:
        checks.append(f"explorer_patient.dataset && {allowed_literal}::text[]")
    condition = " AND ".join(checks) or "TRUE"
    if table in ("patient_labelled", "image_study_labelled", "image_series_labelled"):
        source = "ONLY public.patient AS explorer_patient"
        link = "explorer_patient.patient_id = explorer_row.patient_id"
    else:
        source = (
            "ONLY public.image_series_labelled AS explorer_series "
            "JOIN ONLY public.patient AS explorer_patient USING (patient_id)"
        )
        link = "explorer_series.seriesinstanceuid = explorer_row.seriesinstanceuid"
    predicate = f"EXISTS (SELECT 1 FROM {source} WHERE {link} AND {condition})"
    return f"SELECT * FROM ONLY public.{table} AS explorer_row WHERE {predicate}"


def apply_dataset_scope(source, dataset_literal=None, allowed_literal=None):
    """Rewrite already validated SQL, including joins, CTE bodies and subqueries.

    Validated physical references are public-qualified; CTE references are not.
    Do not revisit generated subqueries, whose joins are fixed module-owned SQL.
    """

    def rewrite(node):
        if isinstance(node, tuple):
            return tuple(rewrite(child) for child in node)
        if not isinstance(node, ast.Node):
            return node
        if isinstance(node, ast.RangeVar) and node.schemaname == "public":
            return ast.RangeSubselect(
                subquery=parse_sql(scoped_table(node.relname, dataset_literal, allowed_literal))[0].stmt,
                alias=node.alias or ast.Alias(aliasname=node.relname),
            )
        if isinstance(node, ast.ColumnRef) and len(node.fields) >= 3:
            first, second = node.fields[:2]
            if (
                isinstance(first, ast.String)
                and first.sval == "public"
                and isinstance(second, ast.String)
                and second.sval in TABLES
            ):
                node.fields = node.fields[1:]
        for field in node:
            setattr(node, field, rewrite(getattr(node, field)))
        return node

    return RawStream()(rewrite(parse_sql(source)))
