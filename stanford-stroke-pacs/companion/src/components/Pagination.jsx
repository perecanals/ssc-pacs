import "./Pagination.css";

export default function Pagination({ page, totalPages, onPageChange }) {
  return (
    <div className="pagination">
      <button
        disabled={page <= 1}
        onClick={() => onPageChange(page - 1)}
        className="pagination__btn"
      >
        &laquo; Prev
      </button>
      <span>
        Page {page} of {totalPages || 1}
      </span>
      <button
        disabled={page >= totalPages}
        onClick={() => onPageChange(page + 1)}
        className="pagination__btn"
      >
        Next &raquo;
      </button>
    </div>
  );
}
