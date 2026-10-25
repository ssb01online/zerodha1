import { Navigate, Route, Routes } from "react-router-dom";
import TopBar from "./components/TopBar";
import InstrumentDetailPage from "./pages/InstrumentDetailPage";
import InstrumentsPage from "./pages/InstrumentsPage";
import LivePage from "./pages/LivePage";
import NfoChainPage from "./pages/NfoChainPage";
import SnapshotPage from "./pages/SnapshotPage";
import SnapshotsPage from "./pages/SnapshotsPage";

export default function App() {
  return (
    <div className="app-shell">
      <TopBar />
      <main className="main">
        <Routes>
          <Route path="/" element={<Navigate to="/portfolio" replace />} />
          <Route path="/portfolio" element={<LivePage />} />
          <Route path="/snapshots" element={<SnapshotsPage />} />
          <Route path="/snapshots/:id" element={<SnapshotPage />} />
          <Route path="/instruments" element={<InstrumentsPage />} />
          <Route
            path="/instruments/nfo/:underlying"
            element={<NfoChainPage />}
          />
          <Route
            path="/instruments/token/:token"
            element={<InstrumentDetailPage />}
          />
          <Route path="*" element={<Navigate to="/portfolio" replace />} />
        </Routes>
      </main>
    </div>
  );
}
