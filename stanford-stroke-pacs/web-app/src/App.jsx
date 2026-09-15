import { Routes, Route, Navigate } from "react-router-dom";
import { AuthProvider } from "./context/AuthContext";
import Landing from "./pages/Landing";
import Navigator from "./pages/Navigator";
import Login from "./pages/Login";
import ChangePassword from "./pages/ChangePassword";
import AdminUsers from "./pages/AdminUsers";
import DataExplorer from "./modules/data-explorer/DataExplorer";
import AdminLabels from "./pages/AdminLabels";
import ProtectedRoute from "./components/ProtectedRoute";

export default function App() {
  return (
    <AuthProvider>
      <Routes>
        <Route path="/login" element={<Login />} />
        <Route
          path="/change-password"
          element={
            <ProtectedRoute>
              <ChangePassword />
            </ProtectedRoute>
          }
        />
        <Route
          path="/"
          element={
            <ProtectedRoute>
              <Landing />
            </ProtectedRoute>
          }
        />
        <Route
          path="/app"
          element={
            <ProtectedRoute>
              <Navigator />
            </ProtectedRoute>
          }
        />
        <Route
          path="/admin"
          element={
            <ProtectedRoute>
              <AdminUsers />
            </ProtectedRoute>
          }
        />
        <Route
          path="/data-exports"
          element={
            <ProtectedRoute>
              <DataExplorer />
            </ProtectedRoute>
          }
        />
        <Route
          path="/admin/data-explorer"
          element={<Navigate to="/data-exports" replace />}
        />
        <Route
          path="/admin/labels"
          element={
            <ProtectedRoute>
              <AdminLabels />
            </ProtectedRoute>
          }
        />
      </Routes>
    </AuthProvider>
  );
}
