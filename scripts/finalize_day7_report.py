"""Xuất báo cáo tiếng Việt từ kết quả đã kiểm chứng; không đổi mã thực nghiệm."""
import json
from pathlib import Path
import sys
import zipfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from rag7.common import read_json,object_hash,require
from evaluation7.selection import LOCK_PATH,verify_current
from evaluation7.report import reviewed_metrics,git_value
from scripts.day6_snapshot import validate as validate_snapshot


def main():
    try:
        lock=read_json(LOCK_PATH);_,manifest,_=verify_current(lock)
        identity=read_json(ROOT/'results/ai_review/test_audit_identity.json')
        with zipfile.ZipFile(ROOT/'results/agent_test_bundle.zip') as z:
            traces=json.loads(z.read('test_traces.json'))
            actual_lock=json.loads(z.read('selection_lock.json'))
        require(actual_lock==lock and identity['selection_lock_seal']==lock['seal_sha256'],'Bản chốt cấu hình không khớp bằng chứng.')
        require(object_hash(traces)==identity['traces_content_sha256'] and len(traces)==14,'Dữ liệu test khác bản đã rà soát.')
        metrics=reviewed_metrics(traces,read_json(ROOT/'results/ai_review/test_agent_review.json'),5)
        reviews=read_json(ROOT/'results/ai_review/test_agent_review.json')
        require(all(r['review'].get('context_grade')==2 if r['evaluation_only']['answerable']
                    else r['review'].get('context_grade') is None for r in reviews),
                'Thiếu/đổi nhãn context test 0/1/2; cần cập nhật báo cáo theo nhãn thật.')
        require(set(metrics)=={'dense_base','bm25_base'},'Cấu hình khác bản báo cáo tiếng Việt.')
        for result in metrics.values():
            require(result['mean']==2 and result['full']=='5/5' and result['context']=='5/5'
                    and result['citations']=='6/6' and result['abstentions']=='2/2'
                    and result['false_abstentions']=='0/5','Nhãn đánh giá đã thay đổi; cần cập nhật nội dung bản dịch trước khi xuất.')
        template=ROOT/'report/templates/day07_vi.md'
        text=template.read_text(encoding='utf-8')
        require(text.count('{{REPOSITORY_URL}}')==1,'Thiếu hoặc trùng vị trí địa chỉ repository trong bản mẫu.')
        origin=git_value(['remote','get-url','origin']) or 'Chưa cấu hình địa chỉ repository cá nhân; bổ sung trước khi nộp.'
        text=text.replace('{{REPOSITORY_URL}}',origin)
        target=ROOT/'report/individual/2A202604103_HoangMinh.md'
        target.parent.mkdir(parents=True,exist_ok=True)
        target.write_text(text,encoding='utf-8',newline='\n')
        print('Đã tạo báo cáo tiếng Việt: '+str(target.relative_to(ROOT)))
        if not (ROOT/'data/day6/index_snapshot.json').exists():
            print('Trước khi nộp: xuất snapshot chỉ mục C theo FINAL_SUBMISSION.md; tệp này cần cho việc khôi phục DB ở máy khác.')
            return 2
        validate_snapshot(read_json(ROOT/'data/day6/index_snapshot.json'),manifest)
        require((ROOT/'requirements-runtime.txt').exists(),'Cần xuất requirements-runtime.txt cùng snapshot.')
        print('Giữ nguyên số liệu, cấu hình đã chốt và bằng chứng. Không gọi API hoặc chạy lại truy xuất.')
        return 0
    except Exception as exc:
        print('Chưa thể tạo báo cáo: '+str(exc));return 1


if __name__=='__main__':raise SystemExit(main())
