import {cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react';
import {afterEach,expect,it,vi} from 'vitest';
import {PaperBoard} from '../PaperBoard';
const request=vi.hoisted(()=>vi.fn());
vi.mock('@/lib/ashare',async original=>({...await original<typeof import('@/lib/ashare')>(),ashareRequest:request}));
afterEach(()=>{cleanup();request.mockReset();});
const portfolio={enabled:true,initial_cash:900000,cash:900000,created_at:'2026-10-21T00:20:00+08:00',checked_at:'2026-10-21T00:20:00+08:00',
  plan_matches:true,plan_identity:{plan_id:'mainboard-2026-10-21',version:1,content_hash:'a'.repeat(64)},
  policy:{warning_pct:12,restrict_pct:16,max_drawdown_pct:20},risk:{equity:900000,pnl:0,drawdown_pct:0,level:'normal',complete:true},
  rows:[{symbol:'001208.SZ',name:'华菱线缆',budget:90000,rules:{breakout:12.39,invalid_below:12.26},position:null,waiting:['公告尚未核验']}],trades:[],note:'纯本地模拟'};
it('starts a virtual portfolio with an explicit pinned plan instead of writing the actual account',async()=>{
  request.mockImplementation(async(path:string)=>path==='/plan'?{focus:{content_hash:'a'.repeat(64)}}:{portfolio:null,error:null});
  render(<PaperBoard/>);
  const button=await screen.findByRole('button',{name:'启动独立模拟组合'});
  await waitFor(()=>expect(button).toBeEnabled());fireEvent.click(button);
  await waitFor(()=>expect(request).toHaveBeenCalledWith('/paper','POST',{initial_cash:900000,expected_plan_hash:'a'.repeat(64)}));
  expect(request.mock.calls.every(c=>!c[0].includes('/account'))).toBe(true);
});
it('labels pending five as unfilled and shows pause action without a real order control',async()=>{
  request.mockResolvedValue({portfolio,error:null});render(<PaperBoard/>);
  expect(await screen.findByText('尚未模拟买入')).toBeInTheDocument();
  expect(screen.getByText(/当前模拟为空仓/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button',{name:'暂停模拟'}));
  await waitFor(()=>expect(request).toHaveBeenCalledWith('/paper/status','PUT',{enabled:false}));
});
