import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import ReviewVideoPlayer from "./ReviewVideoPlayer";


describe("ReviewVideoPlayer", () => {
  beforeEach(() => {
    vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue();
    vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {});
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
  });
});
