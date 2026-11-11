"use client";

import { Shell } from "@/components/shell";
import { InvoiceWorkspace } from "./components/InvoiceWorkspace";

export default function MemberInvoicesPage() {
  return (
    <Shell>
      <InvoiceWorkspace mode="member" />
    </Shell>
  );
}
