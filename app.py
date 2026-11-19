"""Streamlit UI: plain-English requirement -> top 5 suppliers with cited evidence."""

import pandas as pd
import streamlit as st

from supplier_search import FIELDS, NOT_VERIFIED, search_suppliers

st.set_page_config(page_title="Automotive Supplier Discovery", layout="wide")
st.title("Automotive Supplier Discovery")
st.caption(
    "Powered by Exa search. Every field links to the page it came from; "
    f'anything the retrieved text doesn\'t support shows "{NOT_VERIFIED}".'
)

with st.sidebar:
    mode = st.selectbox(
        "Exa search type",
        ["deep", "auto"],
        help="deep runs several searches (fills more fields, slower); auto is a single search.",
    )

query = st.text_input(
    "Supplier requirement",
    placeholder="North American manufacturers of aluminum die-cast components for EV powertrains",
)

if st.button("Search", type="primary"):
    with st.spinner(f"Searching with Exa ({mode})..."):
        run = search_suppliers(query, mode=mode)

    if run.error and not run.suppliers:
        st.error(run.error)
        st.stop()

    cost = f"${run.cost_usd:.4f}" if run.cost_usd is not None else "n/a"
    st.caption(
        f"{len(run.suppliers)} suppliers · {run.latency_s}s · {cost} "
        f"({run.cost_source}) · {len(run.result_urls)} pages retrieved"
    )

    rows = [
        {
            "Supplier": s.name,
            "Website": s.website,
            "Location": s.fields["location"].value,
            "Capabilities": s.fields["capabilities"].value,
            "Certifications mentioned": s.fields["certifications"].value,
        }
        for s in run.suppliers
    ]
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)

    st.subheader("Evidence")
    for s in run.suppliers:
        verified = sum(s.fields[f].verified for f in FIELDS)
        with st.expander(f"{s.name} — {verified}/{len(FIELDS)} fields verified"):
            st.markdown(f"**Website:** {s.website}")
            if s.website_source:
                st.caption(f"Source: {s.website_source}")
            for f in FIELDS:
                ev = s.fields[f]
                st.markdown(f"**{f.capitalize()}:** {ev.value}")
                if not ev.verified:
                    continue
                st.markdown(f"Source: {ev.url}")
                st.markdown(f"> {ev.snippet}")
                notes = []
                if ev.confidence:
                    notes.append(f"Exa grounding confidence: {ev.confidence}")
                notes.append(
                    "quote found verbatim in retrieved text"
                    if ev.quote_matched
                    else "quote not found verbatim in returned highlights — check the source"
                )
                st.caption(" · ".join(notes))
