import { useEffect, useRef, useState } from "react";
import { ApiError, api } from "../api";
import type { ConfigDescribe } from "../configTypes";
import { trapFocus } from "../focus";
import { settingsCopy } from "../settingsCopy";
import type { LocalePreference } from "../uiPrefs";
import {
  ModelFields,
  ScholarFields,
  VlmFields,
  mainModelReady,
  vlmHasValues,
  type ModelDraft,
  type ScholarDraft,
  type VlmDraft,
} from "./settingsFields";

type OnboardingProps = {
  locale: LocalePreference;
  onComplete: () => Promise<void> | void;
};

export function Onboarding({ locale, onComplete }: OnboardingProps) {
  const copy = settingsCopy(locale);
  const dialogRef = useRef<HTMLDivElement>(null);
  const [describe, setDescribe] = useState<ConfigDescribe | null>(null);
  const [model, setModel] = useState<ModelDraft>({
    provider: "openai",
    base_url: "",
    model: "",
    api_key: "",
  });
  const [vlm, setVlm] = useState<VlmDraft>({
    enabled: false,
    endpoint: "",
    model: "",
    api_key: "",
    protocol: "openai_compatible_chat",
    timeout_s: "",
  });
  const [scholar, setScholar] = useState<ScholarDraft>({ api_key: "" });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [testNote, setTestNote] = useState("");

  useEffect(() => {
    void api.describeConfig().then((data) => {
      setDescribe(data);
      const preset = data.catalog.find((item) => item.key === "openai");
      setModel((current) => ({
        ...current,
        base_url: current.base_url || preset?.default_endpoint || "",
        model: current.model || preset?.default_model || "",
      }));
    });
    window.requestAnimationFrame(() => dialogRef.current?.focus());
  }, []);

  const save = async () => {
    if (!mainModelReady(model)) {
      setError(copy.setupIncomplete);
      return;
    }
    setBusy(true);
    setError("");
    try {
      await api.applyModel({
        provider: model.provider,
        base_url: model.provider === "mock" ? "" : model.base_url.trim(),
        model: model.model.trim() || (model.provider === "mock" ? "omni-mock" : ""),
        api_key: model.api_key.trim(),
      });
      if (vlmHasValues(vlm) || vlm.enabled) {
        const timeout = vlm.timeout_s.trim() ? Number(vlm.timeout_s) : undefined;
        await api.applyVlm({
          enabled: vlm.enabled || vlmHasValues(vlm),
          endpoint: vlm.endpoint.trim(),
          model: vlm.model.trim(),
          api_key: vlm.api_key.trim(),
          protocol: vlm.protocol.trim(),
          timeout_s: Number.isFinite(timeout) ? timeout : undefined,
        });
      }
      if (scholar.api_key.trim()) {
        await api.applySemanticScholar({ api_key: scholar.api_key.trim() });
      }
      await onComplete();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : copy.failed);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="modal-backdrop onboarding-backdrop" role="presentation">
      <div
        ref={dialogRef}
        className="modal settings-modal onboarding-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="onboarding-title"
        tabIndex={-1}
        onKeyDown={(event) => {
          if (event.key === "Escape") event.preventDefault();
          trapFocus(event);
        }}
      >
        <header>
          <div>
            <h2 id="onboarding-title">{copy.setupTitle}</h2>
            <p className="muted">{copy.setupLead}</p>
          </div>
        </header>
        <div className="settings-body onboarding-body">
          <section className="settings-block">
            <h3>{copy.mainModel}</h3>
            <ModelFields
              draft={model}
              onChange={setModel}
              catalog={describe?.catalog || []}
              apiKeySet={Boolean(describe?.blocks.model.api_key_set)}
              copy={copy}
            />
            <p className="muted">{copy.mockHint}</p>
          </section>
          <section className="settings-block">
            <h3>{copy.visionModel}</h3>
            <p className="muted">{copy.skipOptional}</p>
            <VlmFields
              draft={vlm}
              onChange={setVlm}
              apiKeySet={Boolean(describe?.blocks.vlm.api_key_set)}
              copy={copy}
            />
          </section>
          <section className="settings-block">
            <h3>{copy.semanticScholar}</h3>
            <p className="muted">{copy.skipOptional}</p>
            <ScholarFields
              draft={scholar}
              onChange={setScholar}
              apiKeySet={Boolean(describe?.blocks.semantic_scholar.api_key_set)}
              copy={copy}
            />
          </section>
          {error ? (
            <p className="banner error" role="alert">
              {error}
            </p>
          ) : null}
          {testNote ? (
            <p className="banner" role="status">
              {testNote}
            </p>
          ) : null}
        </div>
        <footer>
          <button
            type="button"
            className="btn ghost"
            disabled={busy}
            onClick={() => {
              setModel({ provider: "mock", base_url: "", model: "omni-mock", api_key: "" });
            }}
          >
            {copy.useMock}
          </button>
          <button
            type="button"
            className="btn ghost"
            disabled={busy || model.provider === "mock"}
            onClick={() => {
              setTestNote("");
              void api
                .applyModel({
                  provider: model.provider,
                  base_url: model.base_url.trim(),
                  model: model.model.trim(),
                  api_key: model.api_key.trim(),
                })
                .then(() => api.testConfig("model"))
                .then((result) => setTestNote(result.detail))
                .catch((err: unknown) =>
                  setError(err instanceof ApiError ? err.message : copy.failed),
                );
            }}
          >
            {copy.test}
          </button>
          <span className="grow" />
          <button type="button" className="btn" disabled={busy} onClick={() => void save()}>
            {busy ? copy.saving : copy.continue}
          </button>
        </footer>
      </div>
    </div>
  );
}
