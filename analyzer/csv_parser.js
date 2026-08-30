(function () {
  'use strict';

  function parseRows(csvText) {
    const text = String(csvText || '');
    const rows = [];
    let row = [];
    let field = '';
    let inQuotes = false;

    for (let i = 0; i < text.length; i++) {
      const ch = text[i];

      if (inQuotes) {
        if (ch === '"') {
          if (text[i + 1] === '"') {
            field += '"';
            i++;
          } else {
            inQuotes = false;
          }
        } else {
          field += ch;
        }
        continue;
      }

      if (ch === '"') {
        inQuotes = true;
      } else if (ch === ',') {
        row.push(field);
        field = '';
      } else if (ch === '\n') {
        row.push(field);
        rows.push(row);
        row = [];
        field = '';
      } else if (ch === '\r') {
        row.push(field);
        rows.push(row);
        row = [];
        field = '';
        if (text[i + 1] === '\n') i++;
      } else {
        field += ch;
      }
    }

    // Flush tail field/row when needed.
    if (field.length > 0 || row.length > 0 || text.endsWith(',') || text.endsWith('\n') || text.endsWith('\r')) {
      row.push(field);
      rows.push(row);
    }

    return rows;
  }

  function isRowEmpty(row) {
    return !row || row.length === 0 || row.every(cell => String(cell || '').trim() === '');
  }

  function parse(csvText, options) {
    const opts = options || {};
    const header = !!opts.header;
    const skipEmptyLines = !!opts.skipEmptyLines;
    const rows = parseRows(csvText);

    if (!header) {
      const dataRows = skipEmptyLines ? rows.filter(r => !isRowEmpty(r)) : rows;
      return { data: dataRows, meta: { fields: [] } };
    }

    if (rows.length === 0) {
      return { data: [], meta: { fields: [] } };
    }

    const fields = (rows.shift() || []).map((name, idx) => {
      const value = String(name == null ? '' : name);
      // Strip UTF-8 BOM from the first header cell when present.
      return idx === 0 ? value.replace(/^\uFEFF/, '') : value;
    });

    const data = [];
    for (const row of rows) {
      if (skipEmptyLines && isRowEmpty(row)) continue;
      const record = {};
      for (let i = 0; i < fields.length; i++) {
        record[fields[i]] = row[i] != null ? row[i] : '';
      }
      data.push(record);
    }

    return {
      data,
      meta: { fields }
    };
  }

  window.KestrelCsv = { parse };
})();
