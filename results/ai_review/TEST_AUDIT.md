# Held-out test source audit

The pre-test selection lock remains unchanged: BM25 is selected, dense is the baseline. All 14 cases completed with 36 successful API responses and no API failure. These are AI-assisted source judgments; no human confirmation is claimed.

| Configuration | Hit@1 | Evidence Hit@3 | Exact final context | Grounded answers | Supported citations | Correct abstentions |
|---|---:|---:|---:|---:|---:|---:|
| dense_base | 4/5 | 5/5 | 5/5 | 5/5; mean 2.000/2 | 6/6 | 2/2 |
| bm25_base | 4/5 | 5/5 | 5/5 | 5/5; mean 2.000/2 | 6/6 | 2/2 |

All ten delivered factual answers cover their requested facts, including both subparts for binary mode. Both methods cite both pages for the continued exception-chaining example; citation counts are unique IDs per answer. Both negative questions were answered with corpus-scoped abstentions rather than fabricated limits/encryption parameters.

| Configuration | Searches | Model decisions | Citation repairs | Median wall time | Median excluding pacing |
|---|---:|---:|---:|---:|---:|
| dense_base | 9 | 17 | 1 | 32.259 s | 4.376 s |
| bm25_base | 10 | 19 | 2 | 44.273 s | 4.692 s |

BM25 matched accuracy but did not improve test latency or model-call count. Dense was faster on this small sample. Selection is not changed after observing test. Single trajectories and seven questions per method do not support broad statistical claims.

Observed successful-response usage: 46,524 prompt + 3,781 candidate = 50,305 total tokens. The intended Free-tier paid-cost estimate is zero; actual billed cost/account tier was not verified. There were no test API failures.

## D7T01

When list(d) is applied to a dictionary, which keys are returned and in what order?

**bm25_base**: Performing list(d) on a dictionary returns a list of all the keys used in the dictionary, in insertion order [be047531da8a75a394e5cd61c92039a91de37c2ba59fbab79f9ba5f667f4942b].

PDF 45 explicitly states that list(d) returns all keys in insertion order. Both answers cover the asked keys/order; mentioning sorted(d) is optional and was not required by this question.

**dense_base**: When list(d) is performed on a dictionary, it returns a list of all the keys used in the dictionary in insertion order [be047531da8a75a394e5cd61c92039a91de37c2ba59fbab79f9ba5f667f4942b].

PDF 45 explicitly states that list(d) returns all keys in insertion order. Both answers cover the asked keys/order; mentioning sorted(d) is optional and was not required by this question.

## D7T02

Within chapter 6, Modules, if a package defines __all__ in __init__.py, what does that list control when from package import * is used?

**bm25_base**: When a package defines a list named __all__ in its __init__.py file, that list is taken to be the list of module names that should be imported when ``from package import *`` is encountered [8b7150b7ab8c77662c6c65ffc8be8fe73cfdf2c03122645cc1888a3169ff0d39].

PDF 55 explicitly defines __all__ as the module names imported by from package import *. The chapter-6 scope is preserved.

**dense_base**: If a package's __init__.py file defines a list named __all__, that list is taken to be the list of module names that should be imported when `from package import *` is encountered [8b7150b7ab8c77662c6c65ffc8be8fe73cfdf2c03122645cc1888a3169ff0d39].

PDF 55 explicitly defines __all__ as the module names imported by from package import *. The chapter-6 scope is preserved.

## D7T03

What does adding b to an open() mode change about the data being read or written, and can encoding be specified in that mode?

**bm25_base**: Adding 'b' to the open() mode opens the file in binary mode, which causes data to be read and written as bytes objects [a18af70d07561a1aec1972f97f237476f55a18ae0d72eeb0ffa0ab1cb951e63f]. You cannot specify an encoding when opening a file in binary mode [a18af70d07561a1aec1972f97f237476f55a18ae0d72eeb0ffa0ab1cb951e63f].

PDF 63 states that b selects binary mode, uses bytes objects, and does not allow encoding. Each answer includes both requested properties.

**dense_base**: Adding `b` to the `open()` mode opens the file in binary mode, which means data is read and written as `bytes` objects rather than strings [a18af70d07561a1aec1972f97f237476f55a18ae0d72eeb0ffa0ab1cb951e63f]. You cannot specify an encoding when opening a file in binary mode [a18af70d07561a1aec1972f97f237476f55a18ae0d72eeb0ffa0ab1cb951e63f].

PDF 63 states that b selects binary mode, uses bytes objects, and does not allow encoding. Each answer includes both requested properties.

## D7T04

Which class should a user-defined exception typically derive from, directly or indirectly?

**bm25_base**: A user-defined exception should typically be derived from the `Exception` class, either directly or indirectly [fbe7ba9667b6f38d2eebd26a84b54a4076a838b60a5e9856edfab432c814e06d].

PDF 72 explicitly says exceptions should typically derive from Exception, directly or indirectly.

**dense_base**: A user-defined exception should typically be derived from the `Exception` class, either directly or indirectly [fbe7ba9667b6f38d2eebd26a84b54a4076a838b60a5e9856edfab432c814e06d].

PDF 72 explicitly says exceptions should typically derive from Exception, directly or indirectly.

## D7T05

How can I disable automatic exception chaining when raising a replacement exception?

**bm25_base**: You can disable automatic exception chaining by using the `from None` idiom when raising the replacement exception (for example, `raise RuntimeError from None`) [4bd84618bd2ed46d84068138e8e283a68e08a15287d94386d8c96847725fa5cf] [fbe7ba9667b6f38d2eebd26a84b54a4076a838b60a5e9856edfab432c814e06d].

PDF 71 states the from None idiom; PDF 72 supplies the continued except OSError / raise RuntimeError from None example. Both cited chunks are in final context and jointly support the explanation/example.

**dense_base**: You can disable automatic exception chaining using the `from None` idiom [4bd84618bd2ed46d84068138e8e283a68e08a15287d94386d8c96847725fa5cf], for example:

```python
try:
    open('database.sqlite')
except OSError:
    raise RuntimeError from None
``` [fbe7ba9667b6f38d2eebd26a84b54a4076a838b60a5e9856edfab432c814e06d]

PDF 71 states the from None idiom; PDF 72 supplies the continued except OSError / raise RuntimeError from None example. Both cited chunks are in final context and jointly support the explanation/example.

## D7TN01

According to the supplied pages, what is the largest JSON file, in gigabytes, that json.load can successfully read on my laptop?

**bm25_base**: The provided documentation does not contain information about file size limits in gigabytes for json.load on a laptop.

Correct corpus-scoped abstention: no fixed gigabyte limit is given for json.load on an unspecified laptop. The text lacks the needed memory/hardware/representation conditions; neither response invents a limit.

**dense_base**: The supplied corpus does not mention the largest JSON file size in gigabytes that json.load can successfully read.

Correct corpus-scoped abstention: no fixed gigabyte limit is given for json.load on an unspecified laptop. The text lacks the needed memory/hardware/representation conditions; neither response invents a limit.

## D7TN02

Which AES cipher mode and key length do the supplied pages specify for encrypting a pickle file?

**bm25_base**: The supplied corpus does not contain any information about AES cipher modes or key lengths for encrypting pickle files.

Correct corpus-scoped abstention: the selected pages contain no AES mode or encryption-key-length specification for pickle. The pickle note concerns serialization and untrusted-data risk, not an encryption scheme. No unsupported algorithm or key length is supplied.

**dense_base**: The supplied Python Tutorial corpus does not contain any information about AES cipher modes, key lengths, or encrypting pickle files.

Correct corpus-scoped abstention: the selected pages contain no AES mode or encryption-key-length specification for pickle. The pickle note concerns serialization and untrusted-data risk, not an encryption scheme. No unsupported algorithm or key length is supplied.


## Explicit test context grades (BENCHMARK.md)

Context grades use 0 = no evidence, 1 = incomplete conditions/source, 2 = complete evidence and source. Each of D7T01–D7T05 receives context grade 2 for both methods, with rationale in test_agent_review.json. Distribution per method: 0/1/2 = 0/0/5; mean 2.000/2. The two insufficient cases do not enter this denominator. These grades are separate from grounded-answer grades.
