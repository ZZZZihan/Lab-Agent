"use client";

import { Shell } from "@/components/shell";
import { InvoiceWorkspace } from "../../invoices/components/InvoiceWorkspace";

export default function AdminInvoicesPage() {
  return (
    <Shell>
      <InvoiceWorkspace mode="admin" />
    </Shell>
  );
}
