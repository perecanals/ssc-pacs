import { Link } from "react-router-dom";
import "./Landing.css";

const orthancBase = () =>
  `${window.location.protocol}//${window.location.hostname}:8042`;

const cards = [
  {
    to: "/app",
    internal: true,
    icon: "\u{1F517}",
    title: "Companion",
    description:
      "Browse series, create annotation labels, and tag imaging data for research filtering.",
    hint: "Open Companion",
  },
  {
    href: () => `${orthancBase()}/ui/app/`,
    icon: "\u{1F4C1}",
    title: "Orthanc Explorer",
    description:
      "Study-level browser with search, labels, and direct DICOM management.",
    hint: "Open OE2",
  },
  {
    href: () => `${orthancBase()}/ohif/`,
    icon: "\u{1F5BC}",
    title: "OHIF Viewer",
    description:
      "Full-featured DICOM image viewer for CT, MR, and other modalities.",
    hint: "Open OHIF",
  },
];

export default function Landing() {
  return (
    <div className="landing">
      <header className="landing__header">
        <h1 className="landing__title">
          Stanford Stroke Center PACS
        </h1>
        <p className="landing__subtitle">
          Lightweight DICOM management for the Stanford Stroke Center
        </p>
      </header>

      <div className="landing__grid">
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

      <footer className="landing__footer">
        Orthanc runs on port 8042 &middot; Companion on port 8043
      </footer>
    </div>
  );
}
