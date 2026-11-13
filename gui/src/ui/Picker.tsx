import * as Select from "@radix-ui/react-select";
import { Check, ChevronDown } from "lucide-react";
import type { ReactNode } from "react";

export interface PickerOption {
  value: string;
  label: string;
  hint?: string;
  disabled?: boolean;
  icon?: ReactNode;
}

interface PickerProps {
  value: string;
  onChange: (value: string) => void;
  options: PickerOption[];
  /** Spoken name when no visible <label> points at the control. */
  label?: string;
  id?: string;
  placeholder?: string;
  disabled?: boolean;
  size?: "sm" | "md";
}

export function Picker({ value, onChange, options, label, id, placeholder, disabled, size = "md" }: PickerProps) {
  return (
    <Select.Root value={value} onValueChange={onChange} disabled={disabled}>
      <Select.Trigger id={id} className={`picker ${size === "sm" ? "picker-sm" : ""}`} aria-label={label}>
        <Select.Value placeholder={placeholder} />
        <Select.Icon className="picker-caret">
          <ChevronDown size={14} aria-hidden />
        </Select.Icon>
      </Select.Trigger>
      <Select.Portal>
        <Select.Content className="picker-menu" position="popper" sideOffset={6} collisionPadding={8}>
          <Select.Viewport className="picker-view">
            {options.map((option) => (
              <Select.Item
                key={option.value}
                value={option.value}
                disabled={option.disabled}
                className="picker-item"
              >
                {option.icon}
                <span className="picker-item-text">
                  <Select.ItemText>{option.label}</Select.ItemText>
                  {option.hint ? <span className="picker-item-hint">{option.hint}</span> : null}
                </span>
                <Select.ItemIndicator className="picker-check">
                  <Check size={14} aria-hidden />
                </Select.ItemIndicator>
              </Select.Item>
            ))}
          </Select.Viewport>
        </Select.Content>
      </Select.Portal>
    </Select.Root>
  );
}
