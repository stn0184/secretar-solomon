import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import App from "./App.tsx";
import "./styles.css";

const container = document.getElementById("root");
if (!container) {
  throw new Error("Не найден корневой элемент #root");
}

const root = createRoot(container);

// Прототип этапа 011 — только в режиме разработки (prototype/011-repeat/README.md).
// В сборке import.meta.env.DEV = false: ветка и файлы прототипа в бандл не попадают.
if (import.meta.env.DEV && window.location.pathname === `${import.meta.env.BASE_URL}prototype/011`) {
  void import("./prototype/011/Prototype011.tsx").then(({ Prototype011 }) => {
    root.render(
      <StrictMode>
        <Prototype011 />
      </StrictMode>,
    );
  });
} else {
  root.render(
    <StrictMode>
      <App />
    </StrictMode>,
  );
}
