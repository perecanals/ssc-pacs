import PropTypes from "prop-types";
import "./Pagination.css";

export default function Pagination({ page, totalPages, onPageChange }) {
  return (
    <div className="pagination">
      <button
        disabled={page <= 1}
        onClick={() => onPageChange(page - 1)}
        className="pill-btn"
      >
        &laquo; Prev
      </button>
      <span className="pagination__label">
        Page {page} of {totalPages || 1}
      </span>
      <button
        disabled={page >= totalPages}
        onClick={() => onPageChange(page + 1)}
        className="pill-btn"
      >
        Next &raquo;
      </button>
    </div>
  );
}

Pagination.propTypes = {
  page: PropTypes.number.isRequired,
  totalPages: PropTypes.number.isRequired,
  onPageChange: PropTypes.func.isRequired,
};
