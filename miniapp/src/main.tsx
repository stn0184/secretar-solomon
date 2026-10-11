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

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
