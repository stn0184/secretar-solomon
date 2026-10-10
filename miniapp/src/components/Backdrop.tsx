/**
 * Плывущий фон за шапкой (`design.md` §1, «Движение»): два мягких пятна
 * готовым `radial-gradient`, анимируется только `transform` — без `blur`,
 * чтобы слабый телефон не грелся. Цвет и темп — у темы: «Банк-люкс» —
 * тёплый свет, 30–36 с; «Тихая роскошь» — два тёплых пятна, 40–46 с. При
 * `prefers-reduced-motion: reduce` пятна стоят. Нажать на фон нельзя.
 */
export function Backdrop() {
  return (
    <div className="bgfx" aria-hidden="true">
      <i className="blob blob--1" />
      <i className="blob blob--2" />
    </div>
  );
}
