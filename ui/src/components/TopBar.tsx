import { useCallback, useEffect, useState } from "react";
import { NavLink } from "react-router-dom";
import { ApiError, getHealth } from "../api";
import { formatIst } from "../format";
import type { Health } from "../types";

const THEME_KEY = "portfolio_ui_theme_v1";

function readTheme(): "dark" | "light" {
  const t = document.documentElement.getAttribute("data-theme");
  return t === "light" ? "light" : "dark";
}

export default function TopBar() {
  const [health, setHealth] = useState<Health | null>(null);
  const [theme, setTheme] = useState<"dark" | "light">(readTheme);

  const refreshHealth = useCallback(() => {
    getHealth()
      .then(setHealth)
      .catch((err: unknown) => {
        if (err instanceof ApiError) {
          setHealth(null);
        }
      });
  }, []);

  useEffect(() => {
    refreshHealth();
    const id = window.setInterval(refreshHealth, 60_000);
    return () => window.clearInterval(id);
  }, [refreshHealth]);

  function toggleTheme() {
    const next = theme === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    localStorage.setItem(THEME_KEY, next);
    setTheme(next);
  }

  const session = health?.session;
  const sessionOk = session?.valid === true;

  return (
    <header className="topbar">
      <NavLink to="/portfolio" className="topbar-brand">
        Kite tools
      </NavLink>
      <nav className="topbar-nav">
        <NavLink
          to="/portfolio"
          className={({ isActive }) => (isActive ? "active" : undefined)}
        >
          Portfolio
        </NavLink>
        <NavLink
          to="/snapshots"
          className={({ isActive }) => (isActive ? "active" : undefined)}
        >
          Snapshots
        </NavLink>
        <NavLink
          to="/instruments"
          className={({ isActive }) => (isActive ? "active" : undefined)}
        >
          Instruments
        </NavLink>
      </nav>
      <div className="topbar-meta">
        {session?.user_id ? <span>{session.user_id}</span> : null}
        <span className="session-badge" title={session?.message ?? undefined}>
          <span className={`session-dot ${sessionOk ? "ok" : "bad"}`} />
          {sessionOk
            ? `until ${formatIst(session?.expires_at)}`
            : session?.message
              ? "session expired"
              : "session unknown"}
        </span>
        <button
          type="button"
          className="btn btn-ghost btn-sm"
          onClick={toggleTheme}
          aria-label="Toggle theme"
        >
          {theme === "dark" ? "Light" : "Dark"}
        </button>
      </div>
    </header>
  );
}
