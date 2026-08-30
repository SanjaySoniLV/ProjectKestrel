    function parseSecondarySpecies(row) {
      if (row.__secondaryCache) return row.__secondaryCache;
      const listRaw = row.secondary_species_list;
      const scoresRaw = row.secondary_species_scores;
      const result = [];
      if (!listRaw || !scoresRaw) { row.__secondaryCache = result; return result; }
      try {
        let species = null, nums = null;
        // Try JSON first (new format)
        try {
          const ls = String(listRaw).trim();
          const ss = String(scoresRaw).trim();
          if (ls.startsWith('[') && ss.startsWith('[')) {
            const parsed = JSON.parse(ls);
            const parsedScores = JSON.parse(ss);
            if (Array.isArray(parsed) && Array.isArray(parsedScores)) {
              species = parsed; nums = parsedScores;
            }
          }
        } catch (_) { species = null; nums = null; }

        if (species === null) {
          // Legacy numpy repr fallback: numpy uses "..." for names with apostrophes
          species = [];
          const listStr = String(listRaw).replace(/\n\s*/g, ' ');
          const dqRe = /"([^"]+)"/g;
          const sqRe = /'([^']+)'/g;
          let m;
          while ((m = dqRe.exec(listStr)) !== null) { const n = m[1].trim(); if (n) species.push(n); }
          if (!species.length) {
            while ((m = sqRe.exec(listStr)) !== null) { const n = m[1].trim(); if (n) species.push(n); }
          }
          if (!species.length) {
            const inner = listStr.replace(/^\s*\[/, '').replace(/\]\s*$/, '');
            inner.split(/\s{2,}|\s/).forEach(tok => { const t = tok.trim(); if (t) species.push(t); });
          }
          const scoreStr = String(scoresRaw).replace(/^[^\[]*\[/, '[').replace(/\].*$/, '').replace(/[\[\]]/g, '').trim();
          nums = scoreStr.split(/\s+/).map(parseNumber).filter(x => x >= 0);
        }
        for (let i = 0; i < species.length && i < nums.length; i++) {
          result.push({ name: String(species[i]), score: parseNumber(nums[i]) });
        }
      } catch (_) { }
      row.__secondaryCache = result;
      return result;
    }

    // Parse secondary family columns similar to secondary species.
    function parseSecondaryFamilies(row) {
      if (row.__secondaryFamilyCache) return row.__secondaryFamilyCache;
      const listRaw = row.secondary_family_list;
      const scoresRaw = row.secondary_family_scores;
      const result = [];
      if (!listRaw || !scoresRaw) { row.__secondaryFamilyCache = result; return result; }
      try {
        let fams = null, nums = null;
        // Try JSON first
        try {
          const ls = String(listRaw).trim();
          const ss = String(scoresRaw).trim();
          if (ls.startsWith('[') && ss.startsWith('[')) {
            const parsed = JSON.parse(ls);
            const parsedScores = JSON.parse(ss);
            if (Array.isArray(parsed) && Array.isArray(parsedScores)) {
              fams = parsed; nums = parsedScores;
            }
          }
        } catch (_) { fams = null; nums = null; }

        if (fams === null) {
          // Legacy numpy repr fallback
          fams = [];
          const listStr = String(listRaw).replace(/\n\s*/g, ' ');
          const dqRe = /"([^"]+)"/g;
          const sqRe = /'([^']+)'/g;
          let m;
          while ((m = dqRe.exec(listStr)) !== null) { const n = m[1].trim(); if (n) fams.push(n); }
          if (!fams.length) {
            while ((m = sqRe.exec(listStr)) !== null) { const n = m[1].trim(); if (n) fams.push(n); }
          }
          if (!fams.length) {
            const inner = listStr.replace(/^\s*\[/, '').replace(/\]\s*$/, '');
            inner.split(/\s{2,}|\s/).forEach(tok => { const t = tok.trim(); if (t) fams.push(t); });
          }
          const scoreStr = String(scoresRaw).replace(/^[^\[]*\[/, '[').replace(/\].*$/, '').replace(/[\[\]]/g, '').trim();
          nums = scoreStr.split(/\s+/).map(parseNumber).filter(x => x >= 0);
        }
        for (let i = 0; i < fams.length && i < nums.length; i++) {
          result.push({ name: String(fams[i]), score: parseNumber(nums[i]) });
        }
      } catch (_) { }
      row.__secondaryFamilyCache = result;
      return result;
    }

