/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { Controller, useForm } from "react-hook-form";
import { Lightbulb } from "lucide-react";
import { Button } from "@plane/propel/button";
import { TOAST_TYPE, setToast } from "@plane/propel/toast";
import type { IFormattedInstanceConfiguration, TInstanceAIConfigurationKeys } from "@plane/types";
import { CustomSelect, Input } from "@plane/ui";
// components
import type { TControllerInputFormField } from "@/components/common/controller-input";
import { ControllerInput } from "@/components/common/controller-input";
// hooks
import { useInstance } from "@/hooks/store";

type IInstanceAIForm = {
  config: IFormattedInstanceConfiguration;
};

type AIFormValues = Record<TInstanceAIConfigurationKeys, string>;

// biplane: any OpenAI-compatible endpoint works — pick a preset (which fills the base
// URL) or "Custom" to type your own. An operator can pin their own default at the top
// of this list at build time via VITE_LLM_DEFAULT_LABEL + VITE_LLM_DEFAULT_BASE_URL.
type TProviderPreset = { id: string; label: string; provider: string; base: string; editable?: boolean };

const DEFAULT_LABEL = import.meta.env.VITE_LLM_DEFAULT_LABEL as string | undefined;
const DEFAULT_BASE = import.meta.env.VITE_LLM_DEFAULT_BASE_URL as string | undefined;

const PROVIDER_PRESETS: TProviderPreset[] = [
  ...(DEFAULT_LABEL && DEFAULT_BASE
    ? [{ id: "default", label: DEFAULT_LABEL, provider: "custom", base: DEFAULT_BASE }]
    : []),
  { id: "openai", label: "OpenAI", provider: "openai", base: "" },
  { id: "openrouter", label: "OpenRouter", provider: "custom", base: "https://openrouter.ai/api/v1" },
  { id: "together", label: "Together AI", provider: "custom", base: "https://api.together.xyz/v1" },
  { id: "groq", label: "Groq", provider: "custom", base: "https://api.groq.com/openai/v1" },
  { id: "custom", label: "Custom (OpenAI-compatible)", provider: "custom", base: "", editable: true },
];

// Map a saved base URL back to the preset that produced it (else "Custom").
const presetForBase = (base: string): TProviderPreset => {
  if (!base) return PROVIDER_PRESETS.find((p) => p.id === "openai")!;
  return PROVIDER_PRESETS.find((p) => p.base === base && !p.editable) ?? PROVIDER_PRESETS.find((p) => p.editable)!;
};

export function InstanceAIForm(props: IInstanceAIForm) {
  const { config } = props;
  // store
  const { updateInstanceConfigurations } = useInstance();
  // form data
  const {
    handleSubmit,
    control,
    watch,
    setValue,
    formState: { errors, isSubmitting },
  } = useForm<AIFormValues>({
    defaultValues: {
      LLM_API_KEY: config["LLM_API_KEY"],
      LLM_MODEL: config["LLM_MODEL"],
      LLM_PROVIDER: config["LLM_PROVIDER"] || "openai",
      LLM_API_BASE: config["LLM_API_BASE"] || "",
    },
  });

  const selectedPreset = presetForBase(watch("LLM_API_BASE") || "");
  const isCustom = Boolean(selectedPreset.editable);

  const aiFormFields: TControllerInputFormField[] = [
    {
      key: "LLM_MODEL",
      type: "text",
      label: "LLM Model",
      description: <>The model name your endpoint expects, e.g. gpt-4o-mini.</>,
      placeholder: "gpt-4o-mini",
      error: Boolean(errors.LLM_MODEL),
      required: false,
    },
    {
      key: "LLM_API_KEY",
      type: "password",
      label: "API key",
      description: <>The API key for the endpoint above.</>,
      placeholder: "sk-…",
      error: Boolean(errors.LLM_API_KEY),
      required: false,
    },
  ];

  const onSubmit = async (formData: AIFormValues) => {
    const payload: Partial<AIFormValues> = { ...formData };
    await updateInstanceConfigurations(payload)
      .then(() =>
        setToast({
          type: TOAST_TYPE.SUCCESS,
          title: "Success",
          message: "AI Settings updated successfully",
        })
      )
      .catch((err) => console.error(err));
  };

  return (
    <div className="space-y-8">
      <div className="space-y-3">
        <div>
          <div className="pb-1 text-18 font-medium text-primary">AI model provider</div>
          <div className="text-13 font-regular text-tertiary">
            Any OpenAI-compatible endpoint — OpenAI itself, a hosted gateway, or your own server.
          </div>
        </div>

        <div className="grid-col grid w-full grid-cols-1 items-start justify-between gap-x-12 gap-y-8 lg:grid-cols-3">
          {/* Provider preset */}
          <div className="flex flex-col gap-1">
            <h4 className="text-13 text-tertiary">Provider</h4>
            <Controller
              control={control}
              name="LLM_PROVIDER"
              render={({ field: { value, onChange } }) => (
                <CustomSelect
                  value={selectedPreset.id}
                  label={selectedPreset.label}
                  onChange={(presetId: string) => {
                    const preset = PROVIDER_PRESETS.find((p) => p.id === presetId);
                    if (!preset) return;
                    onChange(preset.provider);
                    setValue("LLM_API_BASE", preset.base);
                  }}
                  buttonClassName="border-subtle"
                  input
                >
                  {PROVIDER_PRESETS.map((p) => (
                    <CustomSelect.Option key={p.id} value={p.id}>
                      {p.label}
                    </CustomSelect.Option>
                  ))}
                </CustomSelect>
              )}
            />
          </div>

          {/* Base URL — editable only for the Custom preset */}
          <div className="flex flex-col gap-1">
            <h4 className="text-13 text-tertiary">Base URL</h4>
            <Controller
              control={control}
              name="LLM_API_BASE"
              render={({ field: { value, onChange } }) => (
                <Input
                  id="LLM_API_BASE"
                  type="text"
                  value={value}
                  onChange={(e) => onChange(e.target.value)}
                  placeholder="https://api.openai.com/v1"
                  className="w-full border border-subtle !bg-surface-1"
                  disabled={!isCustom}
                />
              )}
            />
            <span className="text-11 text-tertiary">
              {isCustom ? "Enter your OpenAI-compatible endpoint." : "Set by the selected provider."}
            </span>
          </div>

          {aiFormFields.map((field) => (
            <ControllerInput
              key={field.key}
              control={control}
              type={field.type}
              name={field.key}
              label={field.label}
              description={field.description}
              placeholder={field.placeholder}
              error={field.error}
              required={field.required}
            />
          ))}
        </div>
      </div>

      <div className="flex flex-col items-start gap-4">
        <Button variant="primary" size="lg" onClick={handleSubmit(onSubmit)} loading={isSubmitting}>
          {isSubmitting ? "Saving" : "Save changes"}
        </Button>
      </div>
    </div>
  );
}
