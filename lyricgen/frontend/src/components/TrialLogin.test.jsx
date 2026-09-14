import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { I18nProvider } from "../i18n";
import LoginPage from "./LoginPage";

vi.mock("../env", () => ({ APP_ENV: "trial" }));
afterEach(cleanup);

it("private trial retains invited login without offering public registration", () => {
  render(<I18nProvider><LoginPage onLogin={vi.fn()} /></I18nProvider>);
  expect(screen.getByPlaceholderText("Tu usuario")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Ingresar", exact: true })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /No tenés cuenta/i })).not.toBeInTheDocument();
});
