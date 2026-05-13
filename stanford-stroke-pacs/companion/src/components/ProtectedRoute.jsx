import PropTypes from "prop-types";
import { Navigate } from "react-router-dom";
import { useAuth } from "../context/AuthContext";

export default function ProtectedRoute({ children }) {
  const { currentUser, loading, wasAuthedRef } = useAuth();

  if (loading) return <div className="protected-loading" />;
  if (!currentUser) {
    const suffix = wasAuthedRef.current ? "?expired=1" : "";
    return <Navigate to={`/login${suffix}`} replace />;
  }
  return children;
}

ProtectedRoute.propTypes = {
  children: PropTypes.node,
};
