from copy import deepcopy
import json
import unittest

from sfdc_agent.prompt_projection import source_for_prompt, contract_for_prompt, _contract_projection


class PromptProjectionTests(unittest.TestCase):
    def test_source_dedup_preserves_authored_text_state_overrides_and_original_receipt(self):
        coverage={'status':'partial','unsupported':[{'feature':'paint.variable','nodeId':'1:2'}]}
        source={'nodes':[{'id':'1:2','text':'private authored text --nb- must remain','visible':True,
            'styleOverrideTable':{'2':{'fontSize':14}},'strokes':[{'visible':False}]}],
            'sourceEvidence':{'schemaVersion':2,'coverage':coverage},'evidenceCoverage':deepcopy(coverage)}
        original=deepcopy(source);projected=source_for_prompt(source)
        self.assertEqual(source,original)
        self.assertEqual(projected['nodes'],source['nodes'])
        self.assertEqual(projected['sourceEvidence'],source['sourceEvidence'])
        self.assertEqual(projected['evidenceCoverageRef'],'sourceEvidence.coverage')
        different={**source,'evidenceCoverage':{'status':'unavailable'}}
        self.assertEqual(source_for_prompt(different),different)
        for left,right in [(1,True),(0,False),(None,0),(1,'1')]:
            typed={'sourceEvidence':{'coverage':{'unsupportedCount':left}},'evidenceCoverage':{'unsupportedCount':right}}
            self.assertIs(source_for_prompt(typed),typed)

    def test_generation_defers_only_unused_prose_and_never_omits_api_or_reuses_stale_revision(self):
        contract={'version':'1.0.0','modules':[
            {'name':'novaButton','kind':'ui','tag':'c-nova-button','properties':[{'name':'disabled','type':'boolean','defaultSource':'false','description':'selected guidance'}]},
            {'name':'novaOther','kind':'ui','tag':'c-nova-other','properties':[{'name':'value','type':'string','defaultSource':"''",'description':'unused verbose guidance '*100}],
             'eventDescriptions':{'change':'unused repeated details '*100},'events':['change'],'slots':['default'],'styleHooks':['--nova-color-surface']},
            {'name':'novaTokens','kind':'style','styleHooks':['--nova-color-surface']}]}
        original=deepcopy(contract);context={'verifiedComponents':[{'tag':'c-nova-button'}]}
        _contract_projection.cache_clear()
        selected=contract_for_prompt(contract,context,stage='generation')
        self.assertEqual(contract,original)
        self.assertEqual(selected['modules'][0],contract['modules'][0])
        self.assertEqual([m['name'] for m in selected['modules']],[m['name'] for m in contract['modules']])
        other=selected['modules'][1]
        for key in ['events','slots','styleHooks']:
            self.assertEqual(other[key],contract['modules'][1][key])
        self.assertEqual(other['properties'][0]['defaultSource'],"''")
        self.assertLess(len(json.dumps(selected)),len(json.dumps(contract))/2)
        again=contract_for_prompt(contract,context,stage='generation')
        self.assertEqual(again,selected);self.assertGreater(_contract_projection.cache_info().hits,0)
        changed=deepcopy(contract);changed['modules'][0]['properties'][0]['defaultSource']='true'
        self.assertNotEqual(contract_for_prompt(changed,context,stage='generation')['promptProjection']['fullContractSha256'],selected['promptProjection']['fullContractSha256'])
        selected['modules'][0]['properties'].clear()
        self.assertEqual(contract_for_prompt(contract,context,stage='generation'),again)

    def test_analysis_unknown_modules_and_unverified_mappings_keep_full_evidence(self):
        contract={'modules':[{'name':'novaButton','kind':'ui','tag':'c-nova-button'}]}
        for stage,context in [('analysis',{'verifiedComponents':[{'tag':'c-nova-button'}]}),
            ('generation',{'nodes':[{'tag':'c-nova-button'}]}),
            ('generation',{'verifiedComponents':[{'tag':'c-unknown'}]})]:
            self.assertIs(contract_for_prompt(contract,context,stage=stage),contract)
