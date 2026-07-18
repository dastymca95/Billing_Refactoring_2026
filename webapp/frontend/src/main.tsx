import React, { useEffect, useState } from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import PopoutPage, { parsePopoutHash } from "./components/PopoutPage";
import "./styles.css";
import "./brand-refresh.css";
import "./processing-route.css";

function RootRouter() {
  // Phase 2C — hash-based router. We don't need react-router for two
  // routes; this keeps the bundle small and avoids any history conflict
  // with the existing localStorage-driven SPA state.
  const [hash, setHash] = useState(window.location.hash);
  useEffect(() => {
    const onHash = () => setHash(window.location.hash);
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
  const popout = parsePopoutHash(hash);
  if (popout) return <PopoutPage query={popout} />;
  return <App />;
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <RootRouter />
  </React.StrictMode>,
);
