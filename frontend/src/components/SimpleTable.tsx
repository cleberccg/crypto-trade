import { Paper, Table, TableBody, TableCell, TableContainer, TableHead, TableRow } from "@mui/material";

interface SimpleTableProps {
  columns: string[];
  rows: Array<Record<string, unknown>>;
  onRowClick?: (row: Record<string, unknown>) => void;
}

export function SimpleTable({ columns, rows, onRowClick }: SimpleTableProps) {
  return (
    <TableContainer component={Paper} className="fade-in">
      <Table size="small">
        <TableHead>
          <TableRow>
            {columns.map((column) => (
              <TableCell key={column}>{column}</TableCell>
            ))}
          </TableRow>
        </TableHead>
        <TableBody>
          {rows.map((row, idx) => (
            <TableRow
              key={idx}
              hover={Boolean(onRowClick)}
              onClick={() => {
                if (onRowClick) {
                  onRowClick(row);
                }
              }}
              sx={onRowClick ? { cursor: "pointer" } : undefined}
            >
              {columns.map((column) => (
                <TableCell key={column}>{String(row[column] ?? "-")}</TableCell>
              ))}
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </TableContainer>
  );
}
