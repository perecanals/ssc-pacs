import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { apiPost } from "../api/client";
import { useAuth } from "../context/AuthContext";
import "./Login.css";
import "./ChangePassword.css";

const MIN_LENGTH = 8;

export default function ChangePassword() {
  const { currentUser, mustChangePassword, checkAuth } = useAuth();
  const navigate = useNavigate();
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function onSubmit(e) {
    e.preventDefault();
    setError("");
    if (newPassword.length < MIN_LENGTH) {
      setError(`New password must be at least ${MIN_LENGTH} characters.`);
      return;
    }
    // The current password is only collected for a voluntary change; on the
    // forced first-login change the field is hidden and not sent.
    if (!mustChangePassword && newPassword === currentPassword) {
      setError("New password must differ from your current password.");
      return;
    }
    if (newPassword !== confirm) {
      setError("New password and confirmation do not match.");
      return;
    }
    setBusy(true);
    try {
      const res = await apiPost(
        "/api/auth/change-password",
        {
          new_password: newPassword,
          ...(mustChangePassword ? {} : { current_password: currentPassword }),
        },
        { suppressAuthEvent: true },
      );
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || "Could not change password");
      }
      await checkAuth();
      navigate("/", { replace: true });
    } catch (err) {
      setError(err.message || "Could not change password");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login">
      <header className="login__header">
        <h1 className="login__title">Set a new password</h1>
        <p className="login__subtitle">
          {currentUser ? `Signed in as ${currentUser}` : "Choose your password"}
        </p>
      </header>

      {mustChangePassword && (
        <div className="login__banner" role="alert">
          You must change your password before continuing.
        </div>
      )}

      <form className="login__card" onSubmit={onSubmit}>
        {!mustChangePassword && (
          <label className="login__field">
            <span>Current password</span>
            <input
              type="password"
              value={currentPassword}
              onChange={(e) => setCurrentPassword(e.target.value)}
              autoComplete="current-password"
              autoFocus
              required
            />
          </label>
        )}
        <label className="login__field">
          <span>New password</span>
          <input
            type="password"
            value={newPassword}
            onChange={(e) => setNewPassword(e.target.value)}
            autoComplete="new-password"
            minLength={MIN_LENGTH}
            autoFocus={mustChangePassword}
            required
          />
          <span className="change-password__hint">
            At least {MIN_LENGTH} characters.
          </span>
        </label>
        <label className="login__field">
          <span>Confirm new password</span>
          <input
            type="password"
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
            autoComplete="new-password"
            minLength={MIN_LENGTH}
            required
          />
        </label>
        <button
          type="submit"
          className="btn-primary login__submit"
          disabled={busy}
        >
          {busy ? "Saving…" : "Set new password"}
        </button>
        {error && <span className="login__error">{error}</span>}
      </form>
    </div>
  );
}
