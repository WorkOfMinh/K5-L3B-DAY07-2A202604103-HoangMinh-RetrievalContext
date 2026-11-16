"""BM25, rank fusion and context packing. Standard library; no evaluation labels."""
from collections import Counter
import math
import re
import unicodedata
from .common import canonical,require


def validate_filters(filters):
    require(filters is None or (isinstance(filters,dict) and set(filters)=={'chapter'}
            and type(filters['chapter']) is int),'Only an integer chapter scope is supported.')


def validate_k(top_k):require(type(top_k) is int and 1<=top_k<=100,'top_k must be 1-100.')


def tokenize(text):
    # Keep underscore-delimited Python identifiers; punctuation is not indexed as a token.
    return re.findall(r'\w+',unicodedata.normalize('NFKC',text).casefold())


class BM25Index:
    def __init__(self,chunks,k1=1.2,b=.75):
        require(bool(chunks) and len({c['id'] for c in chunks})==len(chunks),'Nonempty unique chunks required.')
        require(isinstance(k1,(int,float)) and math.isfinite(k1) and k1>0,'k1 must be positive.')
        require(isinstance(b,(int,float)) and math.isfinite(b) and 0<=b<=1,'b must be 0-1.')
        self.chunks=list(chunks);self.k1=k1;self.b=b;self.search_calls=0
        self.counts=[Counter(tokenize(c['text'])) for c in chunks]
        self.lengths=[sum(row.values()) for row in self.counts]
        self.avgdl=sum(self.lengths)/len(chunks) or 1
        df=Counter(term for row in self.counts for term in row)
        n=len(chunks)
        self.idf={term:math.log(1+(n-freq+.5)/(freq+.5)) for term,freq in df.items()}

    def search(self,query,*,filters=None,top_k=10):
        validate_filters(filters);validate_k(top_k);self.search_calls+=1
        terms=sorted(set(tokenize(query)));hits=[]
        for chunk,counts,length in zip(self.chunks,self.counts,self.lengths):
            if filters is not None and chunk['metadata'].get('chapter')!=filters['chapter']:continue
            norm=self.k1*(1-self.b+self.b*length/self.avgdl)
            score=sum(self.idf[t]*counts[t]*(self.k1+1)/(counts[t]+norm)
                      for t in terms if counts.get(t))
            if score>0:hits.append({**chunk,'score':score})
        ranked=sorted(hits,key=lambda h:(-h['score'],h['id']))[:top_k]
        return [{**h,'rank':i+1} for i,h in enumerate(ranked)]


def reciprocal_rank_fusion(rankings,rank_constant=60,top_k=10):
    validate_k(top_k)
    require(type(rank_constant) is int and rank_constant>0,'RRF constant must be a positive integer.')
    fused={}
    for method,hits in rankings.items():
        require(len({h['id'] for h in hits})==len(hits),'A component ranking contains duplicate IDs.')
        for rank,hit in enumerate(hits,1):
            entry=fused.setdefault(hit['id'],{'chunk':hit,'score':0.0,'component_ranks':{},'component_scores':{}})
            require(entry['chunk']['text']==hit['text'] and entry['chunk']['source_id']==hit['source_id']
                    and entry['chunk']['page']==hit['page'],'Component source mismatch for the same ID.')
            entry['score']+=1/(rank_constant+rank)
            entry['component_ranks'][method]=rank
            entry['component_scores'][method]=hit['score']
    ordered=sorted(fused.values(),key=lambda e:(-e['score'],e['chunk']['id']))[:top_k]
    return [{**e['chunk'],'rank':rank,'score':e['score'],'component_ranks':e['component_ranks'],
             'component_scores':e['component_scores']} for rank,e in enumerate(ordered,1)]


def pack_context(candidates,*,budget_chars=3500,max_chunks=3):
    require(type(budget_chars) is int and budget_chars>0,'Positive character budget required.')
    require(type(max_chunks) is int and max_chunks>0,'Positive chunk limit required.')
    blocks=[];selected=[];seen=set();excluded=[]
    for hit in candidates:
        if hit['id'] in seen:
            excluded.append({'id':hit['id'],'reason':'duplicate_id'});continue
        seen.add(hit['id'])
        if len(selected)>=max_chunks:
            excluded.append({'id':hit['id'],'reason':'max_chunks'});continue
        header=f"[{hit['id']} | source={hit['source']} | PDF page={hit['page']} | chapter={hit['metadata'].get('chapter')}]"
        block=header+'\n'+hit['text']
        if len('\n\n'.join(blocks+[block]))>budget_chars:
            excluded.append({'id':hit['id'],'reason':'budget'});continue
        selected.append(hit);blocks.append(block)
    text='\n\n'.join(blocks)
    return dict(text=text,chunk_ids=[h['id'] for h in selected],characters=len(text),
        budget_chars=budget_chars,max_chunks=max_chunks,selected_chunks=selected,excluded=excluded,
        policy='whole source chunks in rank order; ID deduplication; preserve code indentation; no truncation')


class RetrievalEngine:
    def __init__(self,chunks,bm25,dense,rank_constant=60):
        self.chunks={c['id']:c for c in chunks};self.bm25=bm25;self.dense=dense
        self.rank_constant=rank_constant

    def _check(self,hits,filters):
        for hit in hits:
            require(hit['id'] in self.chunks,'Retriever returned an unknown chunk ID.')
            expected=self.chunks[hit['id']]
            require(all(hit[k]==expected[k] for k in ['text','source_id','source','page','metadata','start_char','end_char']),
                    'Retriever changed source text or metadata.')
            if filters is not None:require(hit['metadata'].get('chapter')==filters['chapter'],'Scope filter violated.')
        return hits

    def search(self,method,query,*,filters=None,top_k=10,query_vector=None):
        validate_filters(filters);validate_k(top_k)
        require(isinstance(query,str) and bool(query.strip()),'A nonempty query is required.')
        components={}
        if method in {'dense','hybrid_rrf'}:
            require(self.dense is not None and query_vector is not None,'A real pgvector backend and compatible query vector are required.')
            components['dense']=self._check(self.dense.search(query_vector,filters=filters,top_k=top_k),filters)
        if method in {'bm25','hybrid_rrf'}:
            components['bm25']=self._check(self.bm25.search(query,filters=filters,top_k=top_k),filters)
        require(bool(components),'Unknown retrieval method.')
        if method=='hybrid_rrf':
            ranked=reciprocal_rank_fusion(components,self.rank_constant,top_k)
        else:ranked=components[method]
        return dict(method=method,query=query,filter=filters,ranking=ranked,components=components)
