import { useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { useAuth } from "../context/AuthContext";
import "./Login.css";

export default function Login() {
  const { login } = useAuth();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const expired = params.get("expired") === "1";
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function onSubmit(e) {
    e.preventDefault();
    if (!username.trim() || !password) return;
    setBusy(true);
    setError("");
    try {
      await login(username.trim(), password);
      navigate("/", { replace: true });
    } catch (err) {
      setError(err.message || "Login failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login">
      <header className="login__header">
        <h1 className="login__title">Stanford Stroke Center PACS</h1>
        <p className="login__subtitle">Sign in to continue</p>
      </header>

      {expired && (
        <div className="login__banner" role="alert">
          Your session expired. Please sign in again.
        </div>
      )}

      <form className="login__card" onSubmit={onSubmit}>
        <label className="login__field">
          <span>Username</span>
          <input
            type="text"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            autoComplete="username"
            autoFocus
            required
          />
        </label>
        <label className="login__field">
          <span>Password</span>
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="current-password"
            required
          />
        </label>
        <button type="submit" className="btn-primary login__submit" disabled={busy}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
        {error && <span className="login__error">{error}</span>}
      </form>
    </div>
  );
}
