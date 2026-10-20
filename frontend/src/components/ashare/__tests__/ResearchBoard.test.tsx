import {cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react';
import {afterEach,expect,it,vi} from 'vitest';
import {ResearchBoard} from '../ResearchBoard';
const request=vi.hoisted(()=>vi.fn());
vi.mock('@/lib/ashare',async original=>({...await original<typeof import('@/lib/ashare')>(),ashareRequest:request}));
afterEach(()=>{cleanup();request.mockReset();});
const audit={run_id:'research-test',target_date:'2026-10-21',reference_date:'2026-10-20',cutoff:'2026-10-20T23:15:00+08:00',
  content_hash:'a'.repeat(64),candidate_count:2,validation_status:'未经样本外验证',method:{note:'研究分不是概率'},
  coverage:{universe_count:3488,reference_quote_count:3487},rows:[
  {symbol:'600100.SH',name:'入选公司',rank:1,score:60,selected:true,decision:'入选观察；限制独立',score_parts:{catalyst:30},evidence:[]},
  {symbol:'600101.SH',name:'候补公司',rank:2,score:40,selected:false,decision:'直接负面公告，暂停正向候选',score_parts:{risk_penalty:25},evidence:[]}]};
it('shows frozen rejected candidates and keeps missing outcomes unknown',async()=>{
  request.mockResolvedValue({published:audit,draft:null,outcome:null});render(<ResearchBoard/>);
  expect(await screen.findByText('2 · 候补公司')).toBeInTheDocument();
  expect(screen.getByText(/直接负面公告/)).toBeInTheDocument();
  expect(screen.getByText(/未取得数据不补零/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole('checkbox'));expect(screen.queryByText('2 · 候补公司')).not.toBeInTheDocument();
});
it('publishes only the inspected draft hash through the authenticated route',async()=>{
  request.mockResolvedValue({published:null,draft:{content_hash:'b'.repeat(64),plan:{screening:audit,session_date:'2026-10-21'}}});
  render(<ResearchBoard/>);fireEvent.click(await screen.findByRole('button',{name:'查看待发布草案'}));
  fireEvent.click(await screen.findByRole('button',{name:'发布这份统一观察计划'}));
  await waitFor(()=>expect(request).toHaveBeenCalledWith('/screening/publish','POST',{expected_hash:'b'.repeat(64)}));
});
