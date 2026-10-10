export type ActionKind = "done" | "confirm" | "save" | "delete";

/**
 * Главное действие: «Сделано» у задачи, «Подтвердить» у предположения,
 * «Сохранить» в форме правки.
 */
export interface PrimaryAction {
  kind: "done" | "confirm" | "save";
  label: string;
  busyLabel: string;
  onClick: () => void;
}

/** Спокойная кнопка без красного: «Отмена» в форме, «Изменить» в карточке. */
export interface SecondaryAction {
  label: string;
  onClick: () => void;
}

/**
 * Кнопки действий: главное (если есть), спокойная `extra` рядом с ним
 * («Изменить» в карточке) и «Удалить» — или вместо него спокойная
 * `secondary` («Отмена» в форме). Пока действие идёт, все заперты; отказ
 * базы — текстом под кнопками, запись остаётся на месте (инвариант 4).
 *
 * На карточке дела блок прилипает к низу экрана (артборд 05: «Сделано ·
 * Изменить · Удалить»); `inline` — внутри раскрытой записи памяти, кнопки
 * меньше и стоят по месту.
 */
export function Actions({
  busy,
  error,
  primary,
  extra,
  onDelete,
  secondary,
  inline = false,
}: {
  busy: ActionKind | null;
  error: string | null;
  primary?: PrimaryAction;
  extra?: SecondaryAction;
  onDelete?: () => void;
  secondary?: SecondaryAction;
  inline?: boolean;
}) {
  const button = inline ? "btn btn--sm" : "btn";
  return (
    <div className={inline ? "actions actions--inline" : "actions"}>
      <div className="actions__row">
        {primary ? (
          <button
            type="button"
            className={`${button} btn--main`}
            disabled={busy !== null}
            onClick={primary.onClick}
          >
            {busy === primary.kind ? primary.busyLabel : primary.label}
          </button>
        ) : null}
        {extra ? (
          <button type="button" className={button} disabled={busy !== null} onClick={extra.onClick}>
            {extra.label}
          </button>
        ) : null}
        {secondary ? (
          <button
            type="button"
            className={`${button} btn--ghost`}
            disabled={busy !== null}
            onClick={secondary.onClick}
          >
            {secondary.label}
          </button>
        ) : null}
        {onDelete && !secondary ? (
          <button
            type="button"
            className={`${button} btn--danger`}
            disabled={busy !== null}
            onClick={onDelete}
          >
            {busy === "delete" ? "Удаляем…" : "Удалить"}
          </button>
        ) : null}
      </div>
      {error ? (
        <p className="actions__error" role="alert">
          {error}
        </p>
      ) : null}
    </div>
  );
}
