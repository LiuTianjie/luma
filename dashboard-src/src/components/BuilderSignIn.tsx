import type { Lang } from "../types";
import { AddonSignIn } from "./AddonSignIn";

export function BuilderSignIn({ token, challenge, lang }: { token: string; challenge: string; lang: Lang }) {
  return <AddonSignIn addonId="builder" token={token} challenge={challenge} lang={lang} />;
}
