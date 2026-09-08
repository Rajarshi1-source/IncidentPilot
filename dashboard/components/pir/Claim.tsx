import { CitationChip } from "@/components/pir/CitationChip";

export interface Citation {
  readonly kind: string;
  readonly ref: string;
}

export interface ClaimShape {
  readonly text: string;
  readonly citations: readonly Citation[];
}

/**
 * A claim and its citations, inseparably.
 *
 * A Server Component: only the chip's popover needs the browser. The claim text
 * itself never ships a query layer.
 *
 * Note there is no branch for "a claim with no citations". `Claim.citations`
 * has `min_length=1` in the schema, so an uncited claim is unrepresentable
 * upstream — and rendering a fallback here would quietly accommodate the exact
 * thing the whole PIR subsystem makes impossible.
 */
export function Claim({ claim }: { claim: ClaimShape }) {
  return (
    <p className="leading-7">
      {claim.text}
      {claim.citations.map((citation) => (
        <CitationChip key={`${citation.kind}:${citation.ref}`} kind={citation.kind} refId={citation.ref} />
      ))}
    </p>
  );
}
