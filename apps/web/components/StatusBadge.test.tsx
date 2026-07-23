import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { StatusBadge } from "./StatusBadge";

describe("StatusBadge", () => {
  it("renders the provided label", () => {
    render(<StatusBadge label="Operational" />);

    expect(screen.getByText("Operational")).toBeInTheDocument();
  });
});
