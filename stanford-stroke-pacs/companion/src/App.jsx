import { Routes, Route } from "react-router-dom";
import { AuthProvider } from "./context/AuthContext";
import Landing from "./pages/Landing";
import Companion from "./pages/Companion";

export default function App() {
  return (
    <AuthProvider>
      <Routes>
        <Route path="/" element={<Landing />} />
        <Route path="/app" element={<Companion />} />
      </Routes>
    </AuthProvider>
  );
}
