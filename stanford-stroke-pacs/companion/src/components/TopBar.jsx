import PropTypes from "prop-types";
import { Link, useNavigate } from "react-router-dom";
import { useAuth } from "../context/AuthContext";
import "./TopBar.css";

export default function TopBar({ levels = [], level, onLevelChange, toolbarHostRef }) {
  const { currentUser, logout } = useAuth();
  const navigate = useNavigate();

  const handleLogout = async () => {
    await logout();
    navigate("/login", { replace: true });
  };

  return (
    <div className="topbar">
      <div className="topbar__brand">
        <span className="topbar__title">SSC Annotations</span>
        <Link to="/" className="topbar__home-link">
          &larr; Home
        </Link>
      </div>

      <div className="topbar__levels" aria-label="Annotation levels">
        {levels.map((item) => (
          <button
            key={item.key}
            type="button"
            onClick={() => onLevelChange?.(item.key)}
            className={`topbar__level-btn ${
              level === item.key ? "topbar__level-btn--active" : ""
            }`}
          >
            {item.label}
          </button>
        ))}
      </div>

      <div ref={toolbarHostRef} className="topbar__tools" />

      <div className="topbar__actions">
        <span className="topbar__user-info">
          Logged in as <strong className="topbar__user-name">{currentUser}</strong>
        </span>
        <button type="button" onClick={handleLogout} className="btn-outline">
          Log out
        </button>
      </div>
    </div>
  );
}

TopBar.propTypes = {
  levels: PropTypes.arrayOf(PropTypes.shape({ key: PropTypes.string, label: PropTypes.string })),
  level: PropTypes.string,
  onLevelChange: PropTypes.func,
  toolbarHostRef: PropTypes.func,
};
