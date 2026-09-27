/**
 * Шапка экрана: заголовок, подпись под ним и своя «‹ назад».
 *
 * Шапка Telegram (название бота, «✕», его кнопка «назад») — не наша: её
 * рисует мессенджер. Своя стрелка дублирует BackButton, потому что на
 * старом SDK и вне Telegram кнопки мессенджера нет.
 */
export function Header({
  title,
  subtitle,
  onBack,
  backLabel = "Задачи",
}: {
  title: string;
  subtitle?: string;
  onBack?: () => void;
  backLabel?: string;
}) {
  return (
    <header className="head">
      {onBack ? (
        <button type="button" className="back" onClick={onBack}>
          <i aria-hidden="true">‹</i>
          {backLabel}
        </button>
      ) : null}
      <h1 className="page-title">{title}</h1>
      {subtitle ? <p className="page-sub">{subtitle}</p> : null}
    </header>
  );
}
