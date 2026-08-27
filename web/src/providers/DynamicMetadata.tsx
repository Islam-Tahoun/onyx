"use client";

import { useEffect, useMemo } from "react";
import { useSettings } from "@/lib/settings/hooks";

export default function DynamicMetadata() {
  const { enterprise, logoUrl } = useSettings();

  useEffect(() => {
    const title = enterprise?.application_name?.trim() || "CST AI Hub";
    if (document.title !== title) {
      document.title = title;
    }
  }, [enterprise]);

  const favicon = useMemo(() => logoUrl ?? "/favicon.ico", [logoUrl]);

  return <link rel="icon" href={favicon} />;
}
