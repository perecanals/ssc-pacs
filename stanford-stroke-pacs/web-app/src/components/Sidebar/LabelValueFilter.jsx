import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import PropTypes from "prop-types";

// Sidebar row for a select-type label: hover opens a value picker, click pins
// it open. Selected values feed the page-level `filters.labelValues` quick
// filter (merged into `label_filters` by useTableData). The popup is
// position:fixed, anchored to the row via getBoundingClientRect so the
// sidebar's overflow clipping doesn't cut it off.
const HOVER_OPEN_MS = 150;
const HOVER_CLOSE_MS = 200;
const POPUP_WIDTH = 220;
const POPUP_MAX_HEIGHT = 260;

export default function LabelValueFilter({
  label,
  caseCount,
  options,
  selected,
  pinned,
  onToggleValue,
  onClear,
  onTogglePin,
}) {
  const [hovered, setHovered] = useState(false);
  const [pos, setPos] = useState(null);
  const triggerRef = useRef(null);
  const openTimer = useRef(null);
  const closeTimer = useRef(null);

  const visible = pinned || hovered;
  const count = selected.length;

  const computePos = () => {
    const el = triggerRef.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    let left = r.right + 4;
    if (left + POPUP_WIDTH > window.innerWidth - 8) {
      left = Math.max(8, r.left - POPUP_WIDTH - 4);
    }
    const top = Math.min(r.top, window.innerHeight - 8 - POPUP_MAX_HEIGHT);
    setPos({ top: Math.max(8, top), left });
  };

  const handleEnter = () => {
    clearTimeout(closeTimer.current);
    if (pinned) return;
    openTimer.current = setTimeout(() => {
      computePos();
      setHovered(true);
    }, HOVER_OPEN_MS);
  };

  const handleLeave = () => {
    clearTimeout(openTimer.current);
    if (pinned) return;
    closeTimer.current = setTimeout(() => setHovered(false), HOVER_CLOSE_MS);
  };

  const handleClick = () => {
    computePos();
    onTogglePin();
  };

  // A fixed popup detaches from a scrolled trigger; close on scroll/resize.
  useEffect(() => {
    if (!visible) return undefined;
    const onScroll = () => setHovered(false);
    window.addEventListener("scroll", onScroll, true);
    window.addEventListener("resize", onScroll);
    return () => {
      window.removeEventListener("scroll", onScroll, true);
      window.removeEventListener("resize", onScroll);
    };
  }, [visible]);

  // Keep the popup positioned when it becomes pinned from a click.
  useEffect(() => {
    if (pinned) computePos();
  }, [pinned]);

  useEffect(
    () => () => {
      clearTimeout(openTimer.current);
      clearTimeout(closeTimer.current);
    },
    [],
  );

  return (
    <li
      ref={triggerRef}
      className={`sidebar__lvf sidebar__label-item ${
        count ? "sidebar__label-item--active" : "sidebar__label-item--inactive"
      }`}
      onMouseEnter={handleEnter}
      onMouseLeave={handleLeave}
      onClick={handleClick}
      aria-label={label}
      title={count ? selected.join(", ") : undefined}
    >
      <span className="sidebar__label-text">{label}</span>
      <span className="sidebar__label-count">{caseCount}</span>
      {visible &&
        pos &&
        createPortal(
          <div
            className="sidebar__lvf-popup"
            style={{ top: pos.top, left: pos.left, width: POPUP_WIDTH, maxHeight: POPUP_MAX_HEIGHT }}
            onMouseEnter={() => clearTimeout(closeTimer.current)}
            onMouseLeave={handleLeave}
            onClick={(e) => e.stopPropagation()}
          >
            <div className="sidebar__lvf-popup-head">
              <span className="sidebar__lvf-popup-title">{label}</span>
              <button
                type="button"
                className="sidebar__lvf-clear"
                onClick={onClear}
                disabled={count === 0}
              >
                Clear
              </button>
            </div>
            <div className="sidebar__lvf-options">
              {options.length === 0 ? (
                <div className="sidebar__lvf-empty">No values yet</div>
              ) : (
                options.map((opt) => (
                  <label key={opt} className="sidebar__lvf-option">
                    <input
                      type="checkbox"
                      checked={selected.includes(opt)}
                      onChange={() => onToggleValue(opt)}
                      className="sidebar__lvf-checkbox"
                    />
                    <span className="sidebar__lvf-option-label">{opt}</span>
                  </label>
                ))
              )}
            </div>
          </div>,
          document.body,
        )}
    </li>
  );
}

LabelValueFilter.propTypes = {
  label: PropTypes.string.isRequired,
  caseCount: PropTypes.number.isRequired,
  options: PropTypes.arrayOf(PropTypes.string).isRequired,
  selected: PropTypes.arrayOf(PropTypes.string).isRequired,
  pinned: PropTypes.bool.isRequired,
  onToggleValue: PropTypes.func.isRequired,
  onClear: PropTypes.func.isRequired,
  onTogglePin: PropTypes.func.isRequired,
};
