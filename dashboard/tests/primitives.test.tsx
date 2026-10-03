import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { Telemetry } from "../src/types/contract";
import { ConnectionState } from "../src/components/ConnectionState";
import { Figure } from "../src/components/Figure";
import { SourceBadge } from "../src/components/SourceBadge";

describe("Figure", () => {
  it("renders a physical quantity in ink", () => {
    render(<Figure label="Power" value={902.5} unit="W" dp={0} />);
    expect(screen.getByText("903 W")).toBeInTheDocument();
    expect(screen.getByText("903 W")).not.toHaveClass("text-ringgit");
  });

  it("renders money in the accent colour", () => {
    render(<Figure label="Bill" value="118.295" money />);
    expect(screen.getByText("RM 118.30")).toHaveClass("text-ringgit");
  });

  it("uses tabular figures so 1 Hz updates do not jitter", () => {
    render(<Figure label="Power" value={902.5} unit="W" />);
    expect(screen.getByText(/902/)).toHaveClass("figure");
  });

  it("marks a stale figure", () => {
    render(<Figure label="Power" value={902.5} unit="W" stale />);
    expect(screen.getByTestId("figure-power")).toHaveAttribute(
      "data-stale",
      "true",
    );
  });

  it("renders a negative value without dropping the sign", () => {
    render(<Figure label="Residual" value={-1.3} unit="W" />);
    expect(screen.getByText("-1.3 W")).toBeInTheDocument();
  });

  it("renders an explicit unavailable mark for a null value, never a zero", () => {
    // A missing input rendered as "RM 0.00" or "0 g" is an invented
    // figure asserted as fact -- and worse than a blank, because a zero
    // is a number a reader will act on.
    render(<Figure label="Cost per hour" value={null} money />);
    const figure = screen.getByTestId("figure-cost-per-hour");
    expect(figure).toHaveAttribute("data-unavailable", "true");
    expect(figure).toHaveTextContent("—");
    expect(figure).not.toHaveTextContent("RM");
    expect(figure).not.toHaveTextContent("0");
  });

  it("does not give an unavailable money figure the ringgit accent", () => {
    // The accent means "this is an amount". There is no amount here.
    render(<Figure label="Bill" value={null} money />);
    expect(screen.getByText("—")).not.toHaveClass("text-ringgit");
  });

  it("explains the unavailable state on hover", () => {
    render(
      <Figure label="Bill" value={null} money unavailableTitle="No bill yet" />,
    );
    expect(screen.getByText("—")).toHaveAttribute("title", "No bill yet");
  });

  it("marks a present figure as available", () => {
    render(<Figure label="Bill" value="1.00" money />);
    expect(screen.getByTestId("figure-bill")).toHaveAttribute(
      "data-unavailable",
      "false",
    );
  });
});

describe("SourceBadge", () => {
  it("renders LIVE for a device", () => {
    render(<SourceBadge source="device" />);
    expect(screen.getByText("LIVE")).toBeInTheDocument();
  });

  it("renders SIMULATED", () => {
    render(<SourceBadge source="simulator" />);
    expect(screen.getByText("SIMULATED")).toBeInTheDocument();
  });

  it("renders REPLAY", () => {
    render(<SourceBadge source="replay" />);
    expect(screen.getByText("REPLAY")).toBeInTheDocument();
  });

  it("never renders LIVE for a non-device source", () => {
    render(<SourceBadge source="replay" />);
    expect(screen.queryByText("LIVE")).not.toBeInTheDocument();
  });

  it("defeats a type cast to prove LIVE is structurally unreachable", () => {
    render(<SourceBadge source={"live" as Telemetry["source"]} />);
    expect(screen.queryByText("LIVE")).not.toBeInTheDocument();
  });
});

describe("ConnectionState", () => {
  it("is quiet when connected and fresh", () => {
    render(<ConnectionState status="open" stale={false} />);
    expect(screen.getByTestId("connection")).toHaveAttribute(
      "data-state",
      "ok",
    );
  });

  it("announces reconnection", () => {
    render(<ConnectionState status="reconnecting" stale={false} />);
    expect(screen.getByText(/reconnecting/i)).toBeInTheDocument();
  });

  it("announces stale data even while connected", () => {
    render(<ConnectionState status="open" stale />);
    expect(screen.getByText(/no data/i)).toBeInTheDocument();
  });
});
