import { createRef } from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import RequestVideo from "./RequestVideo";

afterEach(cleanup);
it("keeps playback source across signature renewals but reloads a new render", () => {
  const props = { videoRef: createRef(), label: "Video de prueba", renderIdentity: "pending_review:1" };
  const first = "https://r2.test/video.mp4?X-Amz-Date=first&X-Amz-Signature=one";
  const next = "https://r2.test/video.mp4?X-Amz-Date=second&X-Amz-Signature=two";
  const { rerender } = render(<RequestVideo {...props} url={first} />);
  const video = screen.getByLabelText("Video de prueba");
  video.currentTime = 12;
  rerender(<RequestVideo {...props} url={next} />);
  expect(video).toHaveAttribute("src", first);
  expect(video.currentTime).toBe(12);
  rerender(<RequestVideo {...props} renderIdentity="done:2" url={next} />);
  expect(video).toHaveAttribute("src", next);
});
it("shows media failures and retries with the most recent signature", () => {
  const props = { videoRef: createRef(), label: "Video de prueba", renderIdentity: "1" };
  const { rerender } = render(<RequestVideo {...props} url="https://r2.test/video.mp4?X-Amz-Date=old" />);
  rerender(<RequestVideo {...props} url="https://r2.test/video.mp4?X-Amz-Date=new" />);
  fireEvent.error(screen.getByLabelText("Video de prueba"));
  expect(screen.getByRole("alert")).toHaveTextContent("No se pudo cargar el video");
  fireEvent.click(screen.getByRole("button", { name: "Reintentar reproducción" }));
  expect(screen.getByLabelText("Video de prueba")).toHaveAttribute("src", "https://r2.test/video.mp4?X-Amz-Date=new");
});
