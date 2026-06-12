import { Link, useNavigate } from "react-router-dom";
import { useAuth } from "../context/AuthContext";
import "./Landing.css";

const orthancExplorerUrl = () =>
  `${window.location.protocol}//${window.location.hostname}:8042/ui/app/`;

const baseCards = [
  {
    to: "/app",
    internal: true,
    icon: "\u{1F517}",
    title: "Navigator",
    description:
      "Browse series, create annotation labels, and tag imaging data for research filtering.",
    hint: "Open Navigator",
  },
];

const adminOnlyCards = [
  {
    to: "/admin",
    internal: true,
    icon: "\u{1F465}",
    title: "User Access",
    description:
      "Grant per-user access to imaging datasets. Users only see data from datasets they are granted.",
    hint: "Manage access",
  },
  {
    href: () => "/ohif/",
    icon: "\u{1F5BC}",
    title: "OHIF Viewer",
    description:
      "Full-featured DICOM image viewer for CT, MR, and other modalities.",
    hint: "Open OHIF",
  },
  {
    href: orthancExplorerUrl,
    icon: "\u{1F4C1}",
    title: "Orthanc Explorer",
    description:
      "Admin study-level browser with search, labels, and direct DICOM management. Requires direct Orthanc credentials.",
    hint: "Open OE2",
  },
];

export default function Landing() {
  const { currentUser, isAdmin, logout } = useAuth();
  const navigate = useNavigate();
  const cards = isAdmin ? [...baseCards, ...adminOnlyCards] : baseCards;
  const containerClass = cards.length === 1 ? "landing__single" : "landing__grid";

  const handleLogout = async () => {
    await logout();
    navigate("/login", { replace: true });
  };

  return (
    <div className="landing">
      <div className="landing__topbar">
        <div className="landing__topbar-identity">
          <span className="landing__topbar-user">
            Logged in as <strong>{currentUser}</strong>
          </span>
          <button type="button" className="btn-outline" onClick={handleLogout}>
            Log out
          </button>
        </div>
      </div>

      <div className="landing__content">
        <header className="landing__header">
          <h1 className="landing__title">
            Stanford Stroke Center PACS
          </h1>
          <p className="landing__subtitle">
            Lightweight DICOM management for the Stanford Stroke Center
          </p>
        </header>

        <div className={containerClass}>
          {cards.map((c) => {
            const inner = (
              <>
                <span className="landing__card-icon">{c.icon}</span>
                <h2 className="landing__card-title">{c.title}</h2>
                <p className="landing__card-desc">
                  {c.description}
                </p>
                <span className="landing__card-hint">
                  {c.hint} &rarr;
                </span>
              </>
            );

            return c.internal ? (
              <Link key={c.title} to={c.to} className="landing__card">
                {inner}
              </Link>
            ) : (
              <a
                key={c.title}
                href={c.href()}
                target="_blank"
                rel="noopener noreferrer"
                className="landing__card"
              >
                {inner}
              </a>
            );
          })}
        </div>
      </div>
    </div>
  );
}
