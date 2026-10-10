import { useId } from "react";

/**
 * Выбор из вариантов — ряд кнопок на обычных `radio`: подписью служит
 * сама кнопка, выбранная — цветом действия (`design.md` §3). Клавиатура
 * и экранный диктор работают как у любой группы радиокнопок.
 *
 * Тот же выбор служит переключателем вида экрана («По сроку / По сферам»,
 * `variant="view"`) и рядом с переносом, когда вариантов много (сферы в
 * форме, `variant="wrap"`): второй такой компонент не заводится.
 */
export function Choice<T extends string>({
  label,
  options,
  value,
  disabled = false,
  variant,
  onChange,
}: {
  label: string;
  options: readonly (readonly [T, string])[];
  value: T | null;
  disabled?: boolean;
  variant?: "view" | "wrap";
  onChange: (value: T) => void;
}) {
  const name = useId();
  return (
    <div className={variant ? `seg seg--${variant}` : "seg"} role="radiogroup" aria-label={label}>
      {options.map(([key, text]) => (
        <label className="seg__opt" key={key}>
          <input
            type="radio"
            name={name}
            value={key}
            checked={key === value}
            disabled={disabled}
            onChange={() => onChange(key)}
          />
          <span>{text}</span>
        </label>
      ))}
    </div>
  );
}
