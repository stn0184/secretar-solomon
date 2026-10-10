import { hasBackButton } from "../lib/telegram.ts";

/**
 * Шапка экрана: надзаголовок, заголовок и — только когда кнопки «назад»
 * Telegram нет (вне Telegram, SDK старше 6.1) — своя «‹ назад».
 *
 * Шапка Telegram (название бота, «Закрыть», его «‹ Назад», меню ⋯) — не
 * наша: её рисует мессенджер, мы её только красим в фон темы. Надзаголовок
 * в «Банк-люксе» — капителью, в «Тихой роскоши» — строчными (`design.md` §1).
 */
export function Header({
  title,
  eyebrow,
  onBack,
  backLabel = "Назад",
}: {
  title?: string;
  eyebrow?: string;
  onBack?: () => void;
  backLabel?: string;
}) {
  return (
    <header className="hd">
      {onBack && !hasBackButton() ? (
        <button type="button" className="back" onClick={onBack}>
          <i aria-hidden="true">‹</i>
          {backLabel}
        </button>
      ) : null}
      {eyebrow ? <span className="eyebrow">{eyebrow}</span> : null}
      {title ? <h1 className="h1">{title}</h1> : null}
    </header>
  );
}
