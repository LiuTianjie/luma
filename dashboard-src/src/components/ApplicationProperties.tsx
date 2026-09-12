import type { ReactNode } from "react";
import {
  Card,
  CardAction,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
} from "@/components/ui/table";

/** Label/value pairs must keep complete identifiers readable, including unbroken digests. */
export function ApplicationProperties({
  items,
}: {
  items: Array<{ label: string; value: ReactNode }>;
}) {
  return (
    <Table className="table-fixed">
      <TableBody>
        {items.map(({ label, value }) => (
          <TableRow key={label}>
            <TableHead scope="row" className="w-1/3 whitespace-normal">
              {label}
            </TableHead>
            <TableCell className="whitespace-normal wrap-anywhere">
              {value}
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}

export function ApplicationVersionEntry({
  version,
  current,
  image,
  imageLabel,
  submitted,
  submittedLabel,
  stable,
  action,
}: {
  version: string;
  current: boolean;
  image: string;
  imageLabel: string;
  submitted: string;
  submittedLabel: string;
  stable?: ReactNode;
  action: ReactNode;
}) {
  return (
    <Card size="sm" data-current={current || undefined}>
      <CardHeader>
        <CardTitle className="flex flex-wrap items-center gap-2">
          {version}
          {stable}
        </CardTitle>
        <CardAction>{action}</CardAction>
      </CardHeader>
      <CardContent>
        <ApplicationProperties
          items={[
            {
              label: imageLabel,
              value: <code className="wrap-anywhere">{image}</code>,
            },
            { label: submittedLabel, value: submitted },
          ]}
        />
      </CardContent>
    </Card>
  );
}
