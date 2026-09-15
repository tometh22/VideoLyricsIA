import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import ReviewVideoPlayer from "./ReviewVideoPlayer";


describe("ReviewVideoPlayer", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue();
    vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {});
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("keeps native browser controls out of the review image", () => {
    const { container } = render(
      <ReviewVideoPlayer src="https://example.test/video.mp4" />,
    );

    const video = container.querySelector("video");
    expect(video).toBeInTheDocument();
    expect(video).not.toHaveAttribute("controls");
    expect(screen.getByTestId("review-video-controls")).toBeInTheDocument();
  });

  it("offers accessible playback, seek, volume and fullscreen controls", () => {
    render(<ReviewVideoPlayer src="https://example.test/video.mp4" />);

    expect(screen.getByRole("button", { name: "Reproducir" })).toBeInTheDocument();
    expect(screen.getByRole("slider", { name: "Posición del video" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Silenciar" })).toBeInTheDocument();
    expect(screen.getByRole("slider", { name: "Volumen" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Pantalla completa" })).toBeInTheDocument();
  });

  it("plays through the custom control without enabling native chrome", () => {
    render(<ReviewVideoPlayer src="https://example.test/video.mp4" />);

    fireEvent.click(screen.getByRole("button", { name: "Reproducir" }));

    expect(HTMLMediaElement.prototype.play).toHaveBeenCalledOnce();
    expect(screen.getByRole("status")).toHaveTextContent("Preparando video");
  });

  it("reports a rejected immediate play instead of swallowing it", async () => {
    const onError = vi.fn();
    HTMLMediaElement.prototype.play.mockRejectedValueOnce(new Error("not ready"));
    render(
      <ReviewVideoPlayer
        src="https://example.test/video.mp4"
        onError={onError}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Reproducir" }));

    await waitFor(() => expect(onError).toHaveBeenCalledWith(
      expect.objectContaining({ type: "playback-rejected" }),
    ));
  });

  it("recovers a stalled play request with a bounded source refresh", () => {
    vi.useFakeTimers();
    const onError = vi.fn();
    const { container } = render(
      <ReviewVideoPlayer
        src="https://example.test/video.mp4"
        onError={onError}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Reproducir" }));
    fireEvent.stalled(container.querySelector("video"));
    vi.advanceTimersByTime(3000);

    expect(onError).toHaveBeenCalledWith(
      expect.objectContaining({ type: "playback-stalled" }),
    );
  });

  it("preserves Play intent when a retry supplies a fresh source URL", () => {
    const { container, rerender } = render(
      <ReviewVideoPlayer src="https://example.test/video.mp4?v=0" />,
    );
    const video = container.querySelector("video");

    fireEvent.click(screen.getByRole("button", { name: "Reproducir" }));
    rerender(<ReviewVideoPlayer src="https://example.test/video.mp4?v=1" />);
    fireEvent.canPlay(video);

    expect(HTMLMediaElement.prototype.play).toHaveBeenCalledTimes(2);
  });
});
