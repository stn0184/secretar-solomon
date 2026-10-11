import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import App from "./App.tsx";
import { getColorScheme } from "./lib/telegram.ts";
import { applySkin, readCachedChoice, skinOf } from "./lib/theme.ts";
import "./styles.css";

// Первый кадр — сразу в своей теме: выбор, запомненный на устройстве, и схема
// Telegram. Выбор из базы приходит позже и, если он другой, тему поправит.
applySkin(skinOf(readCachedChoice(), getColorScheme()));

const container = document.getElementById("root");
if (!container) {
  throw new Error("Не найден корневой элемент #root");
}

const root = createRoot(container);

// Прототип этапа 036 — только в режиме разработки (prototype/036-clear-task-list/README.md).
// В сборке import.meta.env.DEV = false: ветка и файлы прототипа в бандл не попадают.
if (import.meta.env.DEV && window.location.pathname === `${import.meta.env.BASE_URL}prototype/036`) {
  void import("./prototype/036/Prototype036.tsx").then(({ Prototype036 }) => {
    root.render(
      <StrictMode>
        <Prototype036 />
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
