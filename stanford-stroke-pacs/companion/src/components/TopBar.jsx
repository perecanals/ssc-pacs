import { useState } from "react";
import { Link } from "react-router-dom";
import { useAuth } from "../context/AuthContext";
import "./TopBar.css";

export default function TopBar({ levels = [], level, onLevelChange, toolbarHostRef }) {
  const { currentUser, login, logout } = useAuth();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");

  const handleLogin = async (e) => {
    e.preventDefault();
    if (!username.trim() || !password) return;
    try {
      setError("");
      await login(username.trim(), password);
    } catch (err) {
      setError(err.message);
    }
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
        {currentUser ? (
          <>
            <span className="topbar__user-info">
              Logged in as <strong className="topbar__user-name">{currentUser}</strong>
            </span>
            <button onClick={logout} className="btn-outline">
              Log out
            </button>
          </>
        ) : (
          <form onSubmit={handleLogin} className="topbar__login-form">
            <input
              type="text"
              placeholder="Username"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              className="topbar__login-input"
            />
            <input
              type="password"
              placeholder="Password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="topbar__login-input"
            />
            <button type="submit" className="btn-primary">
              Log in
            </button>
            {error && (
              <span className="topbar__error">{error}</span>
            )}
          </form>
        )}
      </div>
    </div>
  );
}
