import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { I18nProvider } from "./i18n";
import App from "./App";
import GlobalErrorBoundary from "./components/GlobalErrorBoundary";
import { AlertProvider } from "./components/AlertProvider";
import "./index.css";

ReactDOM.createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <GlobalErrorBoundary>
      <BrowserRouter>
        <I18nProvider>
          <AlertProvider>
            <App />
          </AlertProvider>
        </I18nProvider>
      </BrowserRouter>
    </GlobalErrorBoundary>
  </React.StrictMode>
);
