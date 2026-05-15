import PropTypes from "prop-types";
import { Navigate, useLocation } from "react-router-dom";
import { useAuth } from "../context/AuthContext";

export default function ProtectedRoute({ children }) {
  const { currentUser, mustChangePassword, loading, wasAuthedRef } = useAuth();
  const location = useLocation();

  if (loading) return <div className="protected-loading" />;
  if (!currentUser) {
    const suffix = wasAuthedRef.current ? "?expired=1" : "";
    return <Navigate to={`/login${suffix}`} replace />;
  }
  if (mustChangePassword && location.pathname !== "/change-password") {
    return <Navigate to="/change-password" replace />;
  }
  return children;
}

ProtectedRoute.propTypes = {
  children: PropTypes.node,
};
