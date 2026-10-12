import { NavLink, Outlet, Route, Routes } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api, formatUsd, useEvents, type Health, type SpendTotals } from "./api";
import SeriesListPage from "./pages/series/SeriesListPage";
import SeriesPage from "./pages/series/SeriesPage";
import CharacterListPage from "./pages/characters/CharacterListPage";
import CharacterPage from "./pages/characters/CharacterPage";
import LocationListPage from "./pages/locations/LocationListPage";
import LocationPage from "./pages/locations/LocationPage";
import WorkspacePage from "./pages/episodes/WorkspacePage";
import AssemblyPage from "./pages/assembly/AssemblyPage";

export default function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<SeriesListPage />} />
        <Route path="series/:id" element={<SeriesPage />} />
        <Route path="characters" element={<CharacterListPage />} />
        <Route path="characters/:id" element={<CharacterPage />} />
        <Route path="locations" element={<LocationListPage />} />
        <Route path="locations/:id" element={<LocationPage />} />
        <Route path="episodes/:id" element={<WorkspacePage />} />
        <Route path="episodes/:id/assembly" element={<AssemblyPage />} />
        <Route path="*" element={<NotFound />} />
      </Route>
    </Routes>
  );
}

function Layout() {
  useEvents();
  const health = useQuery({ queryKey: ["health"], queryFn: () => api.get<Health>("/health") });
  const spend = useQuery({ queryKey: ["spend"], queryFn: () => api.get<SpendTotals>("/spend") });
  const missingKeys = health.data
    ? [!health.data.fal_configured && "FAL_KEY", !health.data.openrouter_configured && "OPENROUTER_API_KEY"].filter(
        Boolean,
      )
    : [];

  return (
    <div className="app">
      <header className="topbar">
        <NavLink to="/" end className="brand">
          frootio
        </NavLink>
        <nav>
          <NavLink to="/" end>
            Series
          </NavLink>
          <NavLink to="/characters">Characters</NavLink>
          <NavLink to="/locations">Locations</NavLink>
        </nav>
        <div className="status">
          {health.isError && <span className="warn">backend unreachable</span>}
          {missingKeys.length > 0 && <span className="warn">missing {missingKeys.join(", ")}</span>}
          {spend.data && <span title="Total spend, all series">spent {formatUsd(spend.data.total_usd)}</span>}
        </div>
      </header>
      <main>
        <Outlet />
      </main>
    </div>
  );
}

function NotFound() {
  return (
    <section className="page">
      <h1>Not found</h1>
      <p>
        <NavLink to="/">Back to series</NavLink>
      </p>
    </section>
  );
}
