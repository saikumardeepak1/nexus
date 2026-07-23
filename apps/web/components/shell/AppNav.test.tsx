import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { AppNav } from "./AppNav";

const replace = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace }),
  usePathname: () => "/documents",
}));

const logout = vi.fn();
vi.mock("@/lib/api-client", () => ({
  logout: () => logout(),
}));

describe("AppNav", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders links to both the document library and chat routes", () => {
    render(<AppNav />);

    expect(screen.getByRole("link", { name: "Documents" })).toHaveAttribute("href", "/documents");
    expect(screen.getByRole("link", { name: "Chat" })).toHaveAttribute("href", "/chat");
  });

  it("logs out and redirects to /login when the log out button is clicked", () => {
    render(<AppNav />);

    fireEvent.click(screen.getByRole("button", { name: /log out/i }));

    expect(logout).toHaveBeenCalledOnce();
    expect(replace).toHaveBeenCalledWith("/login");
  });
});
