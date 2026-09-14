import PropTypes from "prop-types";
import ConditionValue from "./ConditionValue";

const operators = {
  eq: "equals",
  ne: "does not equal",
  in: "is one of (IN)",
  contains: "contains",
  lt: "less than",
  le: "at most",
  gt: "greater than",
  ge: "at least",
  is_null: "is empty (NULL)",
  not_null: "is not empty",
};
export default function Conditions({
  group,
  columns,
  onChange,
  depth = 0,
  dataset = null,
}) {
  const update = (index, value) =>
    onChange({
      ...group,
      rules: group.rules.map((r, i) => (i === index ? value : r)),
    });
  return (
    <fieldset
      className="explorer__filters"
      aria-label={depth ? "Nested condition group" : "Conditions"}
    >
      <legend>
        {depth ? "Group" : "Conditions"}{" "}
        <select
          aria-label="Filter combination"
          value={group.op}
          onChange={(e) => onChange({ ...group, op: e.target.value })}
        >
          <option value="and">AND — all conditions</option>
          <option value="or">OR — any condition</option>
        </select>
      </legend>
      <label className="explorer__negate-group">
        <input
          type="checkbox"
          checked={group.negated || false}
          onChange={(event) =>
            onChange({ ...group, negated: event.target.checked })
          }
        />
        NOT — exclude matches to this group
      </label>
      {depth === 0 && (
        <p className="explorer__muted">
          Combine conditions with AND or OR. Add groups to nest logic, and use
          NOT to exclude a group’s matches.
        </p>
      )}
      {group.rules.map((rule, i) => (
        <div className="explorer__filter" key={i}>
          {rule.rules ? (
            <Conditions
              group={rule}
              columns={columns}
              depth={depth + 1}
              dataset={dataset}
              onChange={(r) => update(i, r)}
            />
          ) : (
            <>
              <select
                aria-label="Filter column"
                value={rule.column}
                onChange={(e) =>
                  update(i, {
                    ...rule,
                    column: e.target.value,
                    value: rule.op === "in" ? [] : "",
                  })
                }
              >
                {[...columns]
                  .sort((a, b) => a.ref.localeCompare(b.ref))
                  .map((c) => (
                    <option key={c.ref}>{c.ref}</option>
                  ))}
              </select>
              <select
                aria-label="Filter operator"
                value={rule.op}
                onChange={(e) => {
                  const op = e.target.value;
                  const value =
                    op === "in"
                      ? Array.isArray(rule.value)
                        ? rule.value
                        : rule.value === ""
                          ? []
                          : [rule.value]
                      : Array.isArray(rule.value)
                        ? (rule.value[0] ?? "")
                        : rule.value;
                  update(i, { ...rule, op, value });
                }}
              >
                {Object.entries(operators).map(([key, label]) => (
                  <option key={key} value={key}>
                    {label}
                  </option>
                ))}
              </select>
              {!["is_null", "not_null"].includes(rule.op) && (
                <ConditionValue
                  column={columns.find((c) => c.ref === rule.column)}
                  operator={rule.op}
                  dataset={dataset}
                  value={rule.value}
                  onChange={(value) => update(i, { ...rule, value })}
                />
              )}
            </>
          )}
          <button
            className="btn-outline"
            aria-label={
              rule.rules ? "Remove condition group" : "Remove condition"
            }
            onClick={() =>
              onChange({
                ...group,
                rules: group.rules.filter((_, j) => i !== j),
              })
            }
          >
            Remove
          </button>
        </div>
      ))}
      <button
        className="btn-outline"
        disabled={!columns.length}
        onClick={() =>
          onChange({
            ...group,
            rules: [
              ...group.rules,
              { column: columns[0].ref, op: "eq", value: "" },
            ],
          })
        }
      >
        Add condition
      </button>{" "}
      {depth < 3 && (
        <button
          className="btn-outline"
          onClick={() =>
            onChange({
              ...group,
              rules: [...group.rules, { op: "or", rules: [] }],
            })
          }
        >
          Add condition group
        </button>
      )}
    </fieldset>
  );
}
Conditions.propTypes = {
  group: PropTypes.object.isRequired,
  columns: PropTypes.array.isRequired,
  onChange: PropTypes.func.isRequired,
  depth: PropTypes.number,
  dataset: PropTypes.string,
};
