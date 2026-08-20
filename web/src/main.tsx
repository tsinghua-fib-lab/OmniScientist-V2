import React from "react";
import ReactDOM from "react-dom/client";
import { App } from "./App";
import { applyLocale, applyTheme, readUiPrefs } from "./uiPrefs";
import "./styles.css";

const prefs = readUiPrefs();
applyTheme(prefs.theme);
applyLocale(prefs.locale);

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
