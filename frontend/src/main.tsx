import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./app";
import "./styles.css";
import { VIEWS } from "./views";

const root = document.getElementById("root");
if (root) {
  createRoot(root).render(
    <StrictMode>
      <App views={VIEWS} />
    </StrictMode>,
  );
}
