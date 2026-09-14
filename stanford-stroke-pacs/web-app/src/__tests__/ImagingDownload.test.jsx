import { fireEvent, render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import ImagingDownloadButtons from "../components/DataTable/ImagingDownloadButtons";
import {
  downloadFilename,
  downloadNifti,
} from "../components/DataTable/actions";
import { apiFetch } from "../api/client";
vi.mock("../api/client", () => ({ apiFetch: vi.fn() }));

it("decodes Unicode filename* and handles plain filenames without header suffixes", () => {
  expect(
    downloadFilename(
      "attachment; filename=scan.nii.gz; filename*=UTF-8''CTA%C2%B3%5E%E6%B5%8B%E8%AF%95.nii.gz",
      "fallback",
    ),
  ).toBe("CTA³^测试.nii.gz");
  expect(
    downloadFilename('attachment; filename="scan.zip"; size=1', "fallback"),
  ).toBe("scan.zip");
  expect(
    downloadFilename("attachment; filename=scan.zip; size=1", "fallback"),
  ).toBe("scan.zip");
  expect(
    downloadFilename(
      "attachment; filename=scan.zip; filename*=UTF-8''%broken",
      "fallback",
    ),
  ).toBe("scan.zip");
  expect(downloadFilename("", "fallback.zip")).toBe("fallback.zip");
  expect(downloadFilename('attachment; filename=""', "fallback.zip")).toBe(
    "fallback.zip",
  );
});

it("selects the requested imaging format and disables both actions during a download", () => {
  const onDownload = vi.fn();
  const { rerender } = render(
    <ImagingDownloadButtons uid="1.2.3" onDownload={onDownload} />,
  );
  fireEvent.click(screen.getByRole("button", { name: "NIfTI" }));
  expect(onDownload).toHaveBeenLastCalledWith("1.2.3", "nifti");
  fireEvent.click(
    screen.getByRole("button", { name: "Download DICOM as zip" }),
  );
  expect(onDownload).toHaveBeenLastCalledWith("1.2.3");
  rerender(<ImagingDownloadButtons uid="1.2.3" onDownload={onDownload} busy />);
  expect(screen.getByRole("button", { name: "NIfTI" })).toBeDisabled();
  expect(
    screen.getByRole("button", { name: "Download DICOM as zip" }),
  ).toBeDisabled();
});

it("requests the NIfTI endpoint and downloads its encoded filename", async () => {
  apiFetch.mockResolvedValue({
    ok: true,
    headers: new Headers({
      "Content-Disposition": "attachment; filename*=UTF-8''CTA%C2%B3.nii.gz",
    }),
    blob: async () => new Blob(["nifti"]),
  });
  const clicked = [];
  const spy = vi
    .spyOn(HTMLAnchorElement.prototype, "click")
    .mockImplementation(function () {
      clicked.push(this.download);
    });
  vi.stubGlobal("URL", {
    createObjectURL: vi.fn(() => "blob:test"),
    revokeObjectURL: vi.fn(),
  });
  try {
    await downloadNifti("1.2.3");
    expect(apiFetch).toHaveBeenCalledWith("/api/series/1.2.3/nifti");
    expect(clicked).toEqual(["CTA³.nii.gz"]);
  } finally {
    spy.mockRestore();
    vi.unstubAllGlobals();
  }
});

it("shows the API error message when an imaging download fails", async () => {
  apiFetch.mockResolvedValue({
    ok: false,
    json: async () => ({ detail: "Staff or admin access required" }),
  });
  await expect(downloadNifti("1.2.3")).rejects.toThrow(
    "Staff or admin access required",
  );
});
